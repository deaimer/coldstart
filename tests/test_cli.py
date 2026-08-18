"""End-to-end CLI smoke tests via Typer's CliRunner.

These exercise the actual command layer (argument parsing, Rich table
rendering, file output) rather than just the underlying library functions,
since bugs like malformed table rendering only surface at that layer.
"""

from __future__ import annotations

from typer.testing import CliRunner

from coldctl.cli import app

from .helpers import write_job

runner = CliRunner()


def _ingest_sample(tmp_path, db_path):
    job_dir = write_job(
        tmp_path,
        "cli_job",
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
                "checks": {"initial_readiness": True},
                "cost_usd": 0.1,
            }
        ],
    )
    result = runner.invoke(app, ["results", "ingest", str(job_dir), "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    return job_dir


def test_existing_top_level_commands_still_registered():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ["version", "doctor", "validate", "oracle", "view", "task", "results", "reports"]:
        assert command in result.output


def test_results_commands_end_to_end(tmp_path):
    db_path = tmp_path / ".coldstart" / "results.db"
    _ingest_sample(tmp_path, db_path)

    list_runs = runner.invoke(app, ["results", "list-runs", "--db", str(db_path)])
    assert list_runs.exit_code == 0, list_runs.output
    assert "cli_job" in list_runs.output

    list_trials = runner.invoke(app, ["results", "list-trials", "--db", str(db_path)])
    assert list_trials.exit_code == 0, list_trials.output

    show_run = runner.invoke(app, ["results", "show-run", "cli_job", "--db", str(db_path)])
    assert show_run.exit_code == 0, show_run.output
    assert "cli_job" in show_run.output

    show_run_json = runner.invoke(
        app, ["results", "show-run", "cli_job", "--db", str(db_path), "--json"]
    )
    assert show_run_json.exit_code == 0, show_run_json.output


def test_reingesting_via_cli_is_idempotent(tmp_path):
    db_path = tmp_path / ".coldstart" / "results.db"
    job_dir = _ingest_sample(tmp_path, db_path)

    result = runner.invoke(app, ["results", "ingest", str(job_dir), "--db", str(db_path)])
    assert result.exit_code == 0
    assert "0 trial(s) added, 1 updated" in result.output


def test_reports_task_command_writes_files(tmp_path, monkeypatch):
    db_path = tmp_path / ".coldstart" / "results.db"
    _ingest_sample(tmp_path, db_path)
    monkeypatch.chdir(tmp_path)

    public_json = tmp_path / "out" / "public.json"
    result = runner.invoke(
        app,
        [
            "reports",
            "task",
            "--task",
            "artifact-vault-recovery",
            "--system",
            "gpt-5.6-terra__terminus-2",
            "--visibility",
            "public",
            "--format",
            "json",
            "--db",
            str(db_path),
            "--output",
            str(public_json),
        ],
    )
    assert result.exit_code == 0, result.output
    assert public_json.exists()
    assert str(tmp_path) not in public_json.read_text()

    private_md = tmp_path / "out" / "private.md"
    result = runner.invoke(
        app,
        [
            "reports",
            "task",
            "--task",
            "artifact-vault-recovery",
            "--system",
            "gpt-5.6-terra__terminus-2",
            "--visibility",
            "private",
            "--format",
            "markdown",
            "--db",
            str(db_path),
            "--output",
            str(private_md),
        ],
    )
    assert result.exit_code == 0, result.output
    assert private_md.exists()
    assert "cli_job" in private_md.read_text()
