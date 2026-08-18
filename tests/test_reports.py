from __future__ import annotations

import json

from coldctl.results.ingest import ingest_jobs
from coldctl.results.reports import build_report, render_json, render_markdown

from .helpers import write_job

FORBIDDEN_PUBLIC_KEYS = {"attempts_detail", "failed_check_counts", "trajectory", "source_path"}


def _ingest_sample(conn, tmp_path):
    job_dir = write_job(
        tmp_path,
        "report_job",
        trials=[
            {
                "rewards": {
                    "coldstart_pass": 0.0,
                    "functional": 1.0,
                    "durability": 1.0,
                    "state_safety": 1.0,
                    "integrity": 1.0,
                    "evidence": 1.0,
                },
                "checks": {"a_hidden_check_name": False, "another_check": True},
                "cost_usd": 0.42,
            }
        ],
    )
    ingest_jobs(conn, [job_dir])


def test_public_report_has_no_paths_check_names_or_individual_attempts(conn, tmp_path):
    _ingest_sample(conn, tmp_path)
    report = build_report(
        conn, task="artifact-vault-recovery", system="gpt-5.6-terra__terminus-2", visibility="public"
    )
    rendered = render_json(report)

    assert report["visibility"] == "public"
    for forbidden in FORBIDDEN_PUBLIC_KEYS:
        assert forbidden not in report

    # No individual attempts list, only an aggregate count.
    assert isinstance(report["attempts"], int)

    # No hidden verifier check names anywhere in the rendered output.
    assert "a_hidden_check_name" not in rendered
    assert "another_check" not in rendered

    # No local filesystem paths (tmp_path is an absolute path under the
    # pytest tmp dir; it must never leak into the public report).
    assert str(tmp_path) not in rendered

    # No trial/run identifiers.
    assert "report_job" not in rendered
    assert "trial_0" not in rendered

    # Only aggregate-level failure counts, not a per-check breakdown.
    assert set(report["failure_totals"].keys()) == {"failures", "exceptions"}


def test_private_report_includes_individual_attempts_and_failed_checks(conn, tmp_path):
    _ingest_sample(conn, tmp_path)
    report = build_report(
        conn, task="artifact-vault-recovery", system="gpt-5.6-terra__terminus-2", visibility="private"
    )

    assert report["visibility"] == "private"
    assert len(report["attempts"]) == 1
    attempt = report["attempts"][0]
    assert attempt["failed_checks"] == ["a_hidden_check_name"]
    assert attempt["trial_key"].startswith("report_job::")
    assert attempt["artifacts"]
    assert all("sha256" in a for a in attempt["artifacts"])

    rendered_md = render_markdown(report)
    assert "a_hidden_check_name" in rendered_md
    assert "report_job" in rendered_md


def test_json_and_markdown_render_without_error(conn, tmp_path):
    _ingest_sample(conn, tmp_path)
    for visibility in ("public", "private"):
        report = build_report(
            conn, task="artifact-vault-recovery", system="gpt-5.6-terra__terminus-2", visibility=visibility
        )
        as_json = render_json(report)
        parsed = json.loads(as_json)
        assert parsed["visibility"] == visibility

        as_markdown = render_markdown(report)
        assert as_markdown.startswith("# ColdStart task report")


def test_unknown_task_system_raises_clear_error(conn):
    try:
        build_report(conn, task="does-not-exist", system="nope__nope", visibility="public")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "does-not-exist" in str(exc)
