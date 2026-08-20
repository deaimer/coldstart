from __future__ import annotations

import json

from typer.testing import CliRunner

from coldctl.cli import app

from .conftest import TASK_DIR_NAME

runner = CliRunner()

CONFIG_YAML = """
id: cli-test-eval
description: a config used to exercise the eval CLI
benchmark_version: "0.1.0"
status: development
tasks:
  - {task_dir}
systems:
  - provider: openai
    model: openai/gpt-5.6-terra
    agent: terminus-2
    environment: fake-env
    agent_kwargs:
      reasoning_effort: medium
      use_responses_api: true
      max_turns: 30
    api_key_env: OPENAI_API_KEY
    trials_per_task: 2
    estimated_cost_per_trial_usd: 0.25
execution:
  max_concurrent_trials: 1
  max_infra_retries: 1
  max_budget_usd: 2.00
reports:
  private:
    enabled: true
  public:
    enabled: true
""".format(task_dir=TASK_DIR_NAME)


def _write_config(fake_repo):
    path = fake_repo / "eval.yaml"
    path.write_text(CONFIG_YAML)
    return path


def test_eval_validate_passes_with_api_key_present(fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-dummy-value")
    config_path = _write_config(fake_repo)

    result = runner.invoke(app, ["eval", "validate", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "unit-test-dummy-value" not in result.output


def test_eval_validate_fails_when_api_key_missing(fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = _write_config(fake_repo)

    result = runner.invoke(app, ["eval", "validate", str(config_path)])
    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output


def test_eval_validate_reports_missing_task_path(fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    bad_config = CONFIG_YAML.replace(TASK_DIR_NAME, "no-such-task-dir")
    path = fake_repo / "bad.yaml"
    path.write_text(bad_config)

    result = runner.invoke(app, ["eval", "validate", str(path)])
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_eval_plan_json_reports_correct_trial_count_and_cost(fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    config_path = _write_config(fake_repo)

    result = runner.invoke(app, ["eval", "plan", str(config_path), "--json"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["total_planned_trials"] == 2
    assert plan["cost_estimate"]["source"] == "configured_estimate"
    assert plan["cost_estimate"]["total_usd"] == 0.5


def test_eval_run_without_yes_performs_no_execution(fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    config_path = _write_config(fake_repo)

    result = runner.invoke(app, ["eval", "run", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "No trials were run and no money was spent" in result.output
    assert not (fake_repo / ".coldstart" / "runs").exists()


def test_eval_status_on_unknown_run_is_a_clean_error(fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    result = runner.invoke(app, ["eval", "status", "no-such-run"])
    assert result.exit_code == 1
    assert "No such run" in result.output


def test_eval_resume_requires_yes(fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    result = runner.invoke(app, ["eval", "resume", "some-run"])
    assert result.exit_code == 1
    assert "--yes" in result.output


def test_existing_top_level_commands_still_registered_alongside_eval():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ["version", "doctor", "validate", "oracle", "view", "task", "results", "reports", "eval"]:
        assert command in result.output
