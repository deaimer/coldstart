from __future__ import annotations

import json

import pytest

from coldctl.eval import manifest as manifest_module
from coldctl.eval import orchestrator
from coldctl.eval.planner import build_plan
from coldctl.results import db as db_module
from coldctl.results.aggregate import compute_aggregate

from .conftest import make_config
from .fake_harbor import FakeHarborRunner, ScriptedOutcome

COLDSTART = ".coldstart"


def _run(fake_repo, config, script, *, run_id="test-run", budget_ack=False):
    # skip_preflight=True below means no real API key / harbor / docker
    # presence is required for these orchestration-logic tests.
    coldstart_dir = fake_repo / COLDSTART
    phase1_db_path = coldstart_dir / "results.db"
    runner = FakeHarborRunner(script)
    outcome = orchestrator.run_evaluation(
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=phase1_db_path,
        allow_dirty=False,
        budget_ack=budget_ack,
        harbor_runner=runner,
        run_id=run_id,
        skip_preflight=True,
    )
    return outcome, runner


def _plan_trial_ids(fake_repo, config):
    plan = build_plan(config, repo_root=fake_repo, phase1_db_path=fake_repo / COLDSTART / "results.db", allow_dirty=False)
    return [t.trial_id for t in plan.trials]


def test_successful_five_trial_run_completes(fake_repo):
    config = make_config(trials_per_task=5, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {tid: [ScriptedOutcome(kind="passed", cost_usd=0.1)] for tid in trial_ids}

    outcome, runner = _run(fake_repo, config, script)

    assert outcome.state.status == "completed"
    assert len(outcome.state.completed_trial_ids) == 5
    assert outcome.state.pending_trial_ids == []
    assert outcome.state.actual_cost_usd == pytest.approx(0.5)
    assert len(runner.calls) == 5


def test_genuine_task_failure_is_not_retried(fake_repo):
    config = make_config(trials_per_task=1, max_budget_usd=5.0, max_infra_retries=3, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {trial_ids[0]: [ScriptedOutcome(kind="failed", cost_usd=0.1)]}

    outcome, runner = _run(fake_repo, config, script)

    assert outcome.state.status == "completed"
    assert outcome.state.trials[trial_ids[0]].status == "failed"
    assert outcome.state.trials[trial_ids[0]].attempts == 1
    assert len(runner.calls) == 1  # never retried


def test_infra_failure_is_retried_then_succeeds(fake_repo):
    config = make_config(trials_per_task=1, max_budget_usd=5.0, max_infra_retries=3, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {
        trial_ids[0]: [
            ScriptedOutcome(kind="infra_invalid"),
            ScriptedOutcome(kind="infra_invalid"),
            ScriptedOutcome(kind="passed", cost_usd=0.2),
        ]
    }

    outcome, runner = _run(fake_repo, config, script)

    assert outcome.state.status == "completed"
    assert outcome.state.trials[trial_ids[0]].status == "passed"
    assert outcome.state.trials[trial_ids[0]].attempts == 3
    assert outcome.state.invalid_infrastructure_attempts == 2
    assert len(runner.calls) == 3


def test_infra_failure_exhausts_retries_and_run_is_marked_failed(fake_repo):
    config = make_config(trials_per_task=1, max_budget_usd=5.0, max_infra_retries=2, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {trial_ids[0]: [ScriptedOutcome(kind="infra_invalid")] * 5}

    outcome, runner = _run(fake_repo, config, script)

    assert outcome.state.trials[trial_ids[0]].status == "infra_invalid_exhausted"
    # max_infra_retries=2 means 1 initial attempt + 2 retries = 3 total attempts
    assert outcome.state.trials[trial_ids[0]].attempts == 3
    assert outcome.state.status == "failed"


def test_authentication_failure_stops_immediately_without_retry(fake_repo):
    config = make_config(trials_per_task=3, max_budget_usd=5.0, max_infra_retries=5, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {
        trial_ids[0]: [ScriptedOutcome(kind="auth_error")],
        trial_ids[1]: [ScriptedOutcome(kind="passed", cost_usd=0.1)],
        trial_ids[2]: [ScriptedOutcome(kind="passed", cost_usd=0.1)],
    }

    outcome, runner = _run(fake_repo, config, script)

    assert outcome.state.status == "paused"
    assert outcome.state.trials[trial_ids[0]].status == "auth_error_paused"
    assert outcome.state.trials[trial_ids[0]].attempts == 1
    # the run stopped: later trials were never attempted
    assert outcome.state.trials[trial_ids[1]].status == "pending"
    assert len(runner.calls) == 1


def test_unknown_exception_pauses_run_for_review(fake_repo):
    config = make_config(trials_per_task=2, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {
        trial_ids[0]: [ScriptedOutcome(kind="unknown")],
        trial_ids[1]: [ScriptedOutcome(kind="passed", cost_usd=0.1)],
    }

    outcome, runner = _run(fake_repo, config, script)

    assert outcome.state.status == "paused"
    assert outcome.state.trials[trial_ids[0]].status == "unknown_paused"
    assert outcome.state.trials[trial_ids[1]].status == "pending"


def test_malformed_missing_harbor_result_is_infra_invalid(fake_repo):
    config = make_config(trials_per_task=1, max_budget_usd=5.0, max_infra_retries=1, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {
        trial_ids[0]: [
            ScriptedOutcome(kind="missing_result", returncode=1, stderr="segfault, no output"),
            ScriptedOutcome(kind="passed", cost_usd=0.1),
        ]
    }

    outcome, runner = _run(fake_repo, config, script)

    assert outcome.state.status == "completed"
    assert outcome.state.trials[trial_ids[0]].status == "passed"
    assert outcome.state.trials[trial_ids[0]].attempts == 2


def test_budget_exceeded_is_rejected_before_any_trial_runs(fake_repo):
    config = make_config(trials_per_task=5, max_budget_usd=0.01, estimated_cost_per_trial_usd=1.0)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {tid: [ScriptedOutcome(kind="passed", cost_usd=1.0)] for tid in trial_ids}

    with pytest.raises(orchestrator.BudgetExceededError):
        _run(fake_repo, config, script)

    assert not (fake_repo / COLDSTART / "runs").exists()


def test_stops_before_launching_next_trial_once_budget_reached(fake_repo):
    # Estimate is unavailable (no history, no configured estimate) so the
    # only gate is the running actual-cost check between trials.
    config = make_config(trials_per_task=3, max_budget_usd=0.15, estimated_cost_per_trial_usd=None)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {tid: [ScriptedOutcome(kind="passed", cost_usd=0.1)] for tid in trial_ids}

    outcome, runner = _run(fake_repo, config, script, budget_ack=True)

    # First trial costs 0.1 (under budget), second trial pushes to 0.2 (over budget);
    # the third must never be launched.
    assert len(runner.calls) == 2
    assert outcome.state.status == "paused"


def test_missing_api_key_env_var_blocks_run(fake_repo, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = make_config(trials_per_task=1, estimated_cost_per_trial_usd=0.1)
    coldstart_dir = fake_repo / COLDSTART
    with pytest.raises(orchestrator.PreflightError) as excinfo:
        orchestrator.run_evaluation(
            config,
            repo_root=fake_repo,
            coldstart_dir=coldstart_dir,
            phase1_db_path=coldstart_dir / "results.db",
            allow_dirty=False,
            budget_ack=False,
            harbor_runner=FakeHarborRunner({}),
            skip_preflight=False,
        )
    assert any("OPENAI_API_KEY" in p for p in excinfo.value.problems)
    assert not (coldstart_dir / "runs").exists()


def test_automatic_phase1_ingestion_after_run(fake_repo):
    config = make_config(trials_per_task=5, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {tid: [ScriptedOutcome(kind="passed", cost_usd=0.1)] for tid in trial_ids}

    outcome, runner = _run(fake_repo, config, script)

    conn = db_module.connect(fake_repo / COLDSTART / "results.db")
    try:
        aggregate = compute_aggregate(conn, task="fake-task", system="gpt-5.6-terra__terminus-2")
    finally:
        conn.close()
    assert aggregate.attempts == 5
    assert aggregate.passes == 5
    assert all(t.ingested for t in outcome.state.trials.values())


def test_automatic_report_generation_and_public_redaction(fake_repo):
    config = make_config(trials_per_task=5, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {tid: [ScriptedOutcome(kind="passed", cost_usd=0.1, checks={"a_hidden_check": True})] for tid in trial_ids}

    outcome, runner = _run(fake_repo, config, script, run_id="report-run")

    assert outcome.state.private_report.generated
    assert outcome.state.public_report.generated

    public_dir = fake_repo / "reports" / "generated" / "report-run"
    public_files = list(public_dir.glob("*.public.json"))
    assert len(public_files) == 1
    public_report = json.loads(public_files[0].read_text())
    assert public_report["visibility"] == "public"
    assert isinstance(public_report["attempts"], int)
    assert "a_hidden_check" not in public_files[0].read_text()
    assert str(fake_repo) not in public_files[0].read_text()

    private_dir = fake_repo / ".coldstart" / "private-reports" / "report-run"
    private_files = list(private_dir.glob("*.private.json"))
    assert len(private_files) == 1
    private_report = json.loads(private_files[0].read_text())
    assert private_report["visibility"] == "private"
    assert len(private_report["attempts"]) == 5


def test_atomic_state_stays_valid_json_throughout(fake_repo):
    config = make_config(trials_per_task=3, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {tid: [ScriptedOutcome(kind="passed", cost_usd=0.1)] for tid in trial_ids}

    outcome, runner = _run(fake_repo, config, script)
    run_dir = manifest_module.run_dir_for(fake_repo / COLDSTART, outcome.run_id)
    # If any intermediate write were non-atomic/partial this would raise.
    json.loads((run_dir / "state.json").read_text())
    json.loads((run_dir / "manifest.json").read_text())
    for line in (run_dir / "events.jsonl").read_text().splitlines():
        json.loads(line)
