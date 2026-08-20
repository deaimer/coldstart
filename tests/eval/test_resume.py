from __future__ import annotations

import pytest

from coldctl.eval import orchestrator
from coldctl.eval.planner import build_plan

from .conftest import make_config
from .fake_harbor import FakeHarborRunner, ScriptedOutcome

COLDSTART = ".coldstart"


class InterruptingHarborRunner(FakeHarborRunner):
    """Raises KeyboardInterrupt on a specific call, simulating Ctrl+C."""

    def __init__(self, script, *, interrupt_on_call_index: int) -> None:
        super().__init__(script)
        self._interrupt_on = interrupt_on_call_index
        self._n = 0

    def run_trial(self, *, trial, job_name, jobs_dir):
        self._n += 1
        if self._n == self._interrupt_on:
            raise KeyboardInterrupt()
        return super().run_trial(trial=trial, job_name=job_name, jobs_dir=jobs_dir)


def _plan_trial_ids(fake_repo, config):
    plan = build_plan(config, repo_root=fake_repo, phase1_db_path=fake_repo / COLDSTART / "results.db", allow_dirty=False)
    return [t.trial_id for t in plan.trials]


def _create_and_interrupt(fake_repo, config, trial_ids, run_id="resume-test"):
    script = {tid: [ScriptedOutcome(kind="passed", cost_usd=0.1)] for tid in trial_ids}
    runner = InterruptingHarborRunner(script, interrupt_on_call_index=2)
    coldstart_dir = fake_repo / COLDSTART
    with pytest.raises(orchestrator.RunInterrupted):
        orchestrator.run_evaluation(
            config,
            repo_root=fake_repo,
            coldstart_dir=coldstart_dir,
            phase1_db_path=coldstart_dir / "results.db",
            allow_dirty=False,
            budget_ack=False,
            harbor_runner=runner,
            run_id=run_id,
            skip_preflight=True,
        )
    return runner


def test_interruption_marks_run_paused_and_state_is_resumable(fake_repo):
    config = make_config(trials_per_task=3, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    _create_and_interrupt(fake_repo, config, trial_ids)

    from coldctl.eval import manifest as manifest_module

    run_dir = manifest_module.run_dir_for(fake_repo / COLDSTART, "resume-test")
    state = manifest_module.read_state(run_dir)
    assert state.status == "paused"
    assert state.trials[trial_ids[0]].status == "passed"
    # The trial in flight when Ctrl+C landed has an honestly unknown outcome
    # (its status was written "running" right before the interrupted Harbor
    # call, and we never learn what happened to that specific invocation);
    # a later trial not yet reached remains untouched.
    assert state.trials[trial_ids[1]].status == "running"
    assert state.trials[trial_ids[2]].status == "pending"

    # Normalization of a stuck "running" trial back to "pending" happens at
    # the next execute_loop entry, i.e. on resume -- never leaving a trial
    # permanently stranded.
    resume_runner = FakeHarborRunner(
        {trial_ids[1]: [ScriptedOutcome(kind="passed", cost_usd=0.1)],
         trial_ids[2]: [ScriptedOutcome(kind="passed", cost_usd=0.1)]}
    )
    outcome = orchestrator.resume_evaluation(
        "resume-test",
        config,
        repo_root=fake_repo,
        coldstart_dir=fake_repo / COLDSTART,
        phase1_db_path=fake_repo / COLDSTART / "results.db",
        budget_ack=False,
        harbor_runner=resume_runner,
        skip_preflight=True,
    )
    assert outcome.state.status == "completed"
    assert outcome.state.trials[trial_ids[1]].status == "passed"


def test_resume_skips_completed_trials(fake_repo):
    config = make_config(trials_per_task=3, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    _create_and_interrupt(fake_repo, config, trial_ids)

    remaining_script = {trial_ids[1]: [ScriptedOutcome(kind="passed", cost_usd=0.1)],
                         trial_ids[2]: [ScriptedOutcome(kind="passed", cost_usd=0.1)]}
    resume_runner = FakeHarborRunner(remaining_script)
    coldstart_dir = fake_repo / COLDSTART
    outcome = orchestrator.resume_evaluation(
        "resume-test",
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        budget_ack=False,
        harbor_runner=resume_runner,
        skip_preflight=True,
    )

    assert outcome.state.status == "completed"
    assert outcome.state.trials[trial_ids[0]].status == "passed"
    assert outcome.state.trials[trial_ids[1]].status == "passed"
    assert outcome.state.trials[trial_ids[2]].status == "passed"
    # only the two previously-pending trials were (re)run
    assert len(resume_runner.calls) == 2


def test_resume_refuses_when_task_contents_changed(fake_repo):
    config = make_config(trials_per_task=3, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    _create_and_interrupt(fake_repo, config, trial_ids)

    (fake_repo / "fake-task" / "instruction.md").write_text("A totally different instruction now.\n")

    coldstart_dir = fake_repo / COLDSTART
    with pytest.raises(orchestrator.ResumeDriftError):
        orchestrator.resume_evaluation(
            "resume-test",
            config,
            repo_root=fake_repo,
            coldstart_dir=coldstart_dir,
            phase1_db_path=coldstart_dir / "results.db",
            budget_ack=False,
            harbor_runner=FakeHarborRunner({}),
            skip_preflight=True,
        )


def test_resume_is_idempotent_when_invoked_twice(fake_repo):
    config = make_config(trials_per_task=1, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    script = {trial_ids[0]: [ScriptedOutcome(kind="passed", cost_usd=0.1)]}
    coldstart_dir = fake_repo / COLDSTART

    outcome1 = orchestrator.run_evaluation(
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        allow_dirty=False,
        budget_ack=False,
        harbor_runner=FakeHarborRunner(script),
        run_id="idempotent-run",
        skip_preflight=True,
    )
    assert outcome1.state.status == "completed"

    # Resuming an already-completed run twice must be a safe no-op both times.
    empty_runner = FakeHarborRunner({})
    outcome2 = orchestrator.resume_evaluation(
        "idempotent-run",
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        budget_ack=False,
        harbor_runner=empty_runner,
        skip_preflight=True,
    )
    outcome3 = orchestrator.resume_evaluation(
        "idempotent-run",
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        budget_ack=False,
        harbor_runner=empty_runner,
        skip_preflight=True,
    )
    assert outcome2.state.status == "completed"
    assert outcome3.state.status == "completed"
    assert empty_runner.calls == []


def test_resume_preserves_original_run_id(fake_repo):
    config = make_config(trials_per_task=2, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    _create_and_interrupt(fake_repo, config, trial_ids)

    resume_runner = FakeHarborRunner({trial_ids[1]: [ScriptedOutcome(kind="passed", cost_usd=0.1)]})
    coldstart_dir = fake_repo / COLDSTART
    outcome = orchestrator.resume_evaluation(
        "resume-test",
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        budget_ack=False,
        harbor_runner=resume_runner,
        skip_preflight=True,
    )
    assert outcome.run_id == "resume-test"
