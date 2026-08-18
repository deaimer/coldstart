from __future__ import annotations

import json

from coldctl.results.ingest import ingest_job, ingest_jobs

from .helpers import write_job, write_trial


def test_ingest_single_job_creates_expected_rows(conn, tmp_path):
    job_dir = write_job(
        tmp_path,
        "job_a",
        trials=[
            {
                "rewards": {
                    "coldstart_pass": 1.0,
                    "functional": 1.0,
                    "durability": 1.0,
                    "state_safety": 1.0,
                    "integrity": 1.0,
                    "evidence": 1.0,
                },
                "checks": {"initial_readiness": True, "restart_durability": True},
                "cost_usd": 0.5,
            }
        ],
    )
    result = ingest_job(conn, job_dir)
    conn.commit()

    assert result.ok
    assert len(result.trials) == 1

    run_row = conn.execute("SELECT * FROM runs WHERE run_key = ?", ("job_a",)).fetchone()
    assert run_row is not None
    assert run_row["n_total_trials"] == 1

    trial_row = conn.execute("SELECT * FROM trials WHERE run_id = ?", (run_row["id"],)).fetchone()
    assert trial_row is not None
    assert trial_row["strict_pass"] == 1
    assert trial_row["cost_usd"] == 0.5

    dims = {
        row["dimension"]: row["value"]
        for row in conn.execute(
            "SELECT dimension, value FROM dimension_scores WHERE trial_id = ?", (trial_row["id"],)
        ).fetchall()
    }
    assert dims["coldstart_pass"] == 1.0
    assert dims["functional"] == 1.0

    checks = {
        row["check_name"]: row["passed"]
        for row in conn.execute(
            "SELECT check_name, passed FROM verifier_checks WHERE trial_id = ?", (trial_row["id"],)
        ).fetchall()
    }
    assert checks == {"initial_readiness": 1, "restart_durability": 1}

    # Artifact provenance: paths + hashes only, never file contents.
    artifact_rows = conn.execute(
        "SELECT kind, source_path, sha256 FROM artifact_references WHERE trial_id = ?",
        (trial_row["id"],),
    ).fetchall()
    kinds = {row["kind"] for row in artifact_rows}
    assert "trajectory" in kinds
    for row in artifact_rows:
        assert row["sha256"] is not None
        assert len(row["sha256"]) == 64


def test_ingest_is_idempotent_on_reingestion(conn, tmp_path):
    job_dir = write_job(tmp_path, "job_b", trials=[{"rewards": {"coldstart_pass": 0.0}}])

    first = ingest_job(conn, job_dir)
    conn.commit()
    second = ingest_job(conn, job_dir)
    conn.commit()

    assert first.trials[0].created is True
    assert second.trials[0].created is False

    run_count = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    trial_count = conn.execute("SELECT COUNT(*) AS n FROM trials").fetchone()["n"]
    assert run_count == 1
    assert trial_count == 1


def test_ingest_multiple_jobs_via_cli_helper(conn, tmp_path):
    job1 = write_job(tmp_path, "job_c1", trials=[{"rewards": {"coldstart_pass": 1.0}}])
    job2 = write_job(tmp_path, "job_c2", trials=[{"rewards": {"coldstart_pass": 0.0}}])

    results = ingest_jobs(conn, [job1, job2])
    assert all(r.ok for r in results)
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 2


def test_malformed_job_produces_clear_error_without_corrupting_prior_data(conn, tmp_path):
    good_job = write_job(tmp_path, "job_good", trials=[{"rewards": {"coldstart_pass": 1.0}}])

    # A job missing result.json entirely is malformed.
    bad_job_dir = tmp_path / "job_bad"
    bad_job_dir.mkdir()
    (bad_job_dir / "config.json").write_text("{}")

    results = ingest_jobs(conn, [good_job, bad_job_dir])

    good_result, bad_result = results
    assert good_result.ok is True
    assert bad_result.ok is False
    assert bad_result.error  # a clear, non-empty error message
    assert "result.json" in bad_result.error

    # Prior data (the good job) must remain intact.
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE run_key = 'job_bad'"
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE run_key = 'job_good'"
    ).fetchone()["n"] == 1


def test_malformed_trial_json_rolls_back_whole_job(conn, tmp_path):
    job_dir = write_job(tmp_path, "job_partial", trials=[{"rewards": {"coldstart_pass": 1.0}}])
    # Corrupt the trial's result.json after the fact.
    trial_dir = job_dir / "trial_0"
    (trial_dir / "result.json").write_text("{not valid json")

    results = ingest_jobs(conn, [job_dir])
    assert results[0].ok is False
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM trials").fetchone()["n"] == 0


def test_distinguishes_infra_exception_from_trial_failure(conn, tmp_path):
    job_dir = write_job(
        tmp_path,
        "job_mixed",
        n_total_trials=2,
        trials=[
            {
                "trial_dir_name": "failed_trial",
                "rewards": {"coldstart_pass": 0.0, "functional": 0.5},
                "checks": {"initial_readiness": True, "restart_durability": False},
            },
            {
                "trial_dir_name": "errored_trial",
                "exception": {
                    "exception_type": "RuntimeError",
                    "exception_message": "docker compose failed",
                    "exception_traceback": "Traceback (most recent call last): ...",
                    "occurred_at": "2026-01-01T00:00:30.000000Z",
                },
            },
        ],
    )
    ingest_job(conn, job_dir)
    conn.commit()

    rows = {
        row["trial_name"]: row
        for row in conn.execute(
            "SELECT trial_name, strict_pass, coldstart_pass, is_infra_exception, exception_type "
            "FROM trials"
        ).fetchall()
    }
    failed = rows["artifact-vault-recovery__failed_trial"]
    errored = rows["artifact-vault-recovery__errored_trial"]

    assert failed["is_infra_exception"] == 0
    assert failed["strict_pass"] == 0
    assert failed["coldstart_pass"] == 0.0

    assert errored["is_infra_exception"] == 1
    assert errored["exception_type"] == "RuntimeError"
    assert errored["coldstart_pass"] is None
    assert errored["strict_pass"] is None


def test_unknown_reward_and_metric_keys_are_preserved(conn, tmp_path):
    job_dir = write_job(
        tmp_path,
        "job_unknown_metrics",
        trials=[
            {
                "rewards": {
                    "coldstart_pass": 1.0,
                    "functional": 1.0,
                    "novel_future_dimension": 0.42,
                },
                "checks": {"some_new_check": True},
                "extra_agent_result": {"num_retries": 3, "provider_request_id": "req_123"},
            }
        ],
    )
    ingest_job(conn, job_dir)
    conn.commit()

    trial_row = conn.execute("SELECT * FROM trials").fetchone()

    novel = conn.execute(
        "SELECT value FROM dimension_scores WHERE trial_id = ? AND dimension = 'novel_future_dimension'",
        (trial_row["id"],),
    ).fetchone()
    assert novel is not None
    assert novel["value"] == 0.42

    agent_result = json.loads(trial_row["agent_result_json"])
    assert agent_result["num_retries"] == 3
    assert agent_result["provider_request_id"] == "req_123"

    check_row = conn.execute(
        "SELECT passed FROM verifier_checks WHERE trial_id = ? AND check_name = 'some_new_check'",
        (trial_row["id"],),
    ).fetchone()
    assert check_row["passed"] == 1
