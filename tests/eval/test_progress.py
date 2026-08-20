"""Live progress: event stream correctness and rendering.

No test here invokes a real API, Docker, or Harbor evaluation. The
"subprocess capture" tests below launch a tiny, harmless local Python
script (not Harbor) purely to exercise ``SubprocessHarborRunner``'s real
polling/file-capture/interrupt mechanics end to end.
"""

from __future__ import annotations

import io
import sys
import textwrap
from pathlib import Path

import pytest
from rich.console import Console

from coldctl.eval import orchestrator
from coldctl.eval.harbor_runner import SubprocessHarborRunner
from coldctl.eval.planner import TrialSpec, build_plan
from coldctl.eval.progress import ProgressEvent, ProgressRenderer

from .conftest import make_config
from .fake_harbor import FakeHarborRunner, ScriptedOutcome

COLDSTART = ".coldstart"


class FakeClock:
    """A deterministic, manually-advanced clock for tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _plan_trial_ids(fake_repo, config):
    plan = build_plan(config, repo_root=fake_repo, phase1_db_path=fake_repo / COLDSTART / "results.db", allow_dirty=False)
    return [t.trial_id for t in plan.trials]


def test_progress_events_appear_while_fake_harbor_is_still_running(fake_repo):
    config = make_config(trials_per_task=1, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    clock = FakeClock()
    runner = FakeHarborRunner(
        {trial_ids[0]: [ScriptedOutcome(kind="passed", cost_usd=0.1, duration_sec=1.0, tick_sec=0.2)]},
        sleep_fn=clock.sleep,
        clock_fn=clock,
    )
    events: list[ProgressEvent] = []
    coldstart_dir = fake_repo / COLDSTART
    orchestrator.run_evaluation(
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        allow_dirty=False,
        budget_ack=False,
        harbor_runner=runner,
        run_id="progress-run",
        skip_preflight=True,
        progress_callback=events.append,
    )

    # Several ticks must have been observed *while the fake process was
    # still alive* (harbor_status == "active"), not just at the very end.
    active_ticks = [e for e in events if e.harbor_status == "active"]
    assert len(active_ticks) >= 3
    assert all(e.phase in ("agent_running", "verifying") for e in active_ticks)


def test_overall_percentage_is_based_only_on_completed_trials(fake_repo):
    config = make_config(trials_per_task=4, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    runner = FakeHarborRunner({tid: [ScriptedOutcome(kind="passed", cost_usd=0.1)] for tid in trial_ids})
    events: list[ProgressEvent] = []
    coldstart_dir = fake_repo / COLDSTART
    orchestrator.run_evaluation(
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        allow_dirty=False,
        budget_ack=False,
        harbor_runner=runner,
        run_id="percent-run",
        skip_preflight=True,
        progress_callback=events.append,
    )

    # completed/planned must only ever take the exact values 0..4 out of 4,
    # and must reach 4/4 (100%) by the very last event -- never a
    # time-derived estimate.
    percentages = {e.overall_percent for e in events}
    assert percentages <= {0.0, 25.0, 50.0, 75.0, 100.0}
    assert events[-1].completed_trials == 4
    assert events[-1].planned_trials == 4
    assert events[-1].overall_percent == 100.0
    # Percentage must never decrease over the course of the run.
    seen = [e.overall_percent for e in events]
    assert seen == sorted(seen)


def test_current_trial_number_and_elapsed_increase_within_a_trial(fake_repo):
    config = make_config(trials_per_task=1, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    clock = FakeClock()
    runner = FakeHarborRunner(
        {trial_ids[0]: [ScriptedOutcome(kind="passed", cost_usd=0.1, duration_sec=2.0, tick_sec=0.5)]},
        sleep_fn=clock.sleep,
        clock_fn=clock,
    )
    events: list[ProgressEvent] = []
    coldstart_dir = fake_repo / COLDSTART
    orchestrator.run_evaluation(
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        allow_dirty=False,
        budget_ack=False,
        harbor_runner=runner,
        run_id="elapsed-run",
        skip_preflight=True,
        progress_callback=events.append,
    )
    active_ticks = [e for e in events if e.harbor_status == "active"]
    elapsed_values = [e.elapsed_sec for e in active_ticks]
    assert elapsed_values == sorted(elapsed_values)
    assert elapsed_values[-1] > elapsed_values[0]
    assert all(e.current_trial_number == 1 for e in events if e.planned_trials == 1)


def test_phases_appear_in_a_sane_order(fake_repo):
    config = make_config(trials_per_task=1, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    runner = FakeHarborRunner({trial_ids[0]: [ScriptedOutcome(kind="passed", cost_usd=0.1, duration_sec=0.3, tick_sec=0.1)]})
    events: list[ProgressEvent] = []
    coldstart_dir = fake_repo / COLDSTART
    orchestrator.run_evaluation(
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        allow_dirty=False,
        budget_ack=False,
        harbor_runner=runner,
        run_id="phase-run",
        skip_preflight=True,
        progress_callback=events.append,
    )
    phases_seen = [e.phase for e in events]
    for required in ("preparing", "launching_harbor", "ingesting", "generating_reports", "completed"):
        assert required in phases_seen
    assert phases_seen.index("preparing") < phases_seen.index("launching_harbor")
    assert phases_seen.index("ingesting") < phases_seen.index("generating_reports")
    assert phases_seen.index("generating_reports") < phases_seen[::-1].index("completed") * -1 + len(phases_seen)


def test_progress_output_contains_no_secrets(fake_repo, monkeypatch):
    secret = "sk-proj-thisisatotallyfakekeyabcdefghijklmno1234567890"  # pragma: allowlist secret
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    config = make_config(trials_per_task=1, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)
    runner = FakeHarborRunner(
        {trial_ids[0]: [ScriptedOutcome(kind="passed", cost_usd=0.1, duration_sec=0.2, tick_sec=0.05, stdout_during_run=f"Authorization: Bearer {secret}\n")]}
    )
    lines: list[str] = []

    def _collect(event: ProgressEvent) -> None:
        lines.append(event.format_line())
        console = Console(file=io.StringIO(), force_terminal=True, width=200)
        renderer = ProgressRenderer(console=console, interactive=True, verbose=True)
        renderer._render_panel(event)  # constructing the renderable must not raise or embed the secret
        rendered_text = str(renderer._render_panel(event))
        lines.append(rendered_text)

    coldstart_dir = fake_repo / COLDSTART
    orchestrator.run_evaluation(
        config,
        repo_root=fake_repo,
        coldstart_dir=coldstart_dir,
        phase1_db_path=coldstart_dir / "results.db",
        allow_dirty=False,
        budget_ack=False,
        harbor_runner=runner,
        run_id="secret-progress-run",
        skip_preflight=True,
        progress_callback=_collect,
    )
    for line in lines:
        assert secret not in line


def test_non_interactive_renderer_prints_heartbeat_lines():
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=200)
    clock = FakeClock()
    renderer = ProgressRenderer(console=console, interactive=False, heartbeat_interval_sec=5.0, clock_fn=clock)

    base = dict(
        run_id="hb-run", completed_trials=0, planned_trials=2, current_trial_number=1,
        task_name="fake-task", model="openai/gpt-5.6-terra", agent="terminus-2",
    )
    renderer(ProgressEvent(**base, phase="launching_harbor", elapsed_sec=0, harbor_status="not_started"))
    clock.now = 1.0
    renderer(ProgressEvent(**base, phase="agent_running", elapsed_sec=1.0, harbor_status="active"))
    clock.now = 2.0
    renderer(ProgressEvent(**base, phase="agent_running", elapsed_sec=2.0, harbor_status="active"))  # suppressed: no phase change, not due yet
    clock.now = 7.0
    renderer(ProgressEvent(**base, phase="agent_running", elapsed_sec=7.0, harbor_status="active"))  # due: interval elapsed

    output = buffer.getvalue()
    lines = [line for line in output.splitlines() if line.strip()]
    # phase-change line + interval-due line, but not the suppressed one
    assert len(lines) == 3
    assert "hb-run" in output
    assert "overall 0/2" in output


def test_interactive_renderer_updates_live_panel():
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=200)
    with ProgressRenderer(console=console, interactive=True) as renderer:
        renderer(
            ProgressEvent(
                run_id="live-run", completed_trials=0, planned_trials=1, current_trial_number=1,
                task_name="fake-task", model="openai/gpt-5.6-terra", agent="terminus-2",
                phase="agent_running", elapsed_sec=3.5, harbor_status="active",
            )
        )
    output = buffer.getvalue()
    assert "live-run" in output
    assert "agent running" in output


def test_ctrl_c_still_pauses_with_new_progress_wiring(fake_repo):
    config = make_config(trials_per_task=2, max_budget_usd=5.0, estimated_cost_per_trial_usd=0.1)
    trial_ids = _plan_trial_ids(fake_repo, config)

    class InterruptingRunner(FakeHarborRunner):
        def run_trial(self, *, trial, job_name, jobs_dir, on_progress=None):
            if trial.trial_id == trial_ids[0]:
                raise KeyboardInterrupt()
            return super().run_trial(trial=trial, job_name=job_name, jobs_dir=jobs_dir, on_progress=on_progress)

    runner = InterruptingRunner({tid: [ScriptedOutcome(kind="passed", cost_usd=0.1)] for tid in trial_ids})
    events: list[ProgressEvent] = []
    coldstart_dir = fake_repo / COLDSTART
    with pytest.raises(orchestrator.RunInterrupted) as excinfo:
        orchestrator.run_evaluation(
            config,
            repo_root=fake_repo,
            coldstart_dir=coldstart_dir,
            phase1_db_path=coldstart_dir / "results.db",
            allow_dirty=False,
            budget_ack=False,
            harbor_runner=runner,
            run_id="ctrlc-progress-run",
            skip_preflight=True,
            progress_callback=events.append,
        )
    assert excinfo.value.state.status == "paused"
    assert any(e.phase == "preparing" for e in events)


def test_subprocess_harbor_runner_captures_output_and_polls(tmp_path):
    """Exercises the real polling/file-capture mechanism end to end with a
    harmless local script standing in for `harbor` -- not Harbor itself."""
    fake_harbor_dir = tmp_path / "bin"
    fake_harbor_dir.mkdir()
    fake_harbor_script = fake_harbor_dir / "harbor"
    fake_harbor_script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys, time
            print("starting up")
            sys.stdout.flush()
            time.sleep(0.2)
            print("now verifying results")
            sys.stdout.flush()
            time.sleep(0.1)
            sys.exit(0)
            """
        )
    )
    fake_harbor_script.chmod(0o755)

    import os

    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_harbor_dir}{os.pathsep}{old_path}"
    try:
        trial = TrialSpec(
            trial_id="t1", task_path="fake-task", task_name="fake-task", system_key="sys",
            provider="openai", model="openai/gpt-5.6-terra", agent="terminus-2",
            environment="fake-env", agent_kwargs={}, api_key_env="OPENAI_API_KEY", attempt=1,
        )
        jobs_dir = tmp_path / "jobs"
        updates = []
        runner = SubprocessHarborRunner(poll_interval_sec=0.05)
        result = runner.run_trial(trial=trial, job_name="job1", jobs_dir=jobs_dir, on_progress=updates.append)
    finally:
        os.environ["PATH"] = old_path

    assert result.returncode == 0
    assert "starting up" in result.stdout
    assert "now verifying results" in result.stdout
    assert len(updates) >= 2
    assert any(u.harbor_alive for u in updates)
    # No leftover temp capture files.
    assert list(jobs_dir.glob(".*.tmp")) == []


def test_subprocess_harbor_runner_kills_child_on_keyboard_interrupt(tmp_path):
    fake_harbor_dir = tmp_path / "bin"
    fake_harbor_dir.mkdir()
    fake_harbor_script = fake_harbor_dir / "harbor"
    fake_harbor_script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import time
            time.sleep(10)
            """
        )
    )
    fake_harbor_script.chmod(0o755)

    import os
    import time as time_module

    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_harbor_dir}{os.pathsep}{old_path}"
    try:
        trial = TrialSpec(
            trial_id="t1", task_path="fake-task", task_name="fake-task", system_key="sys",
            provider="openai", model="openai/gpt-5.6-terra", agent="terminus-2",
            environment="fake-env", agent_kwargs={}, api_key_env="OPENAI_API_KEY", attempt=1,
        )
        jobs_dir = tmp_path / "jobs"
        runner = SubprocessHarborRunner(poll_interval_sec=0.05)

        call_count = {"n": 0}

        def _raise_after_first(update):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise KeyboardInterrupt()

        start = time_module.monotonic()
        with pytest.raises(KeyboardInterrupt):
            runner.run_trial(trial=trial, job_name="job1", jobs_dir=jobs_dir, on_progress=_raise_after_first)
        elapsed = time_module.monotonic() - start
    finally:
        os.environ["PATH"] = old_path

    # Must return promptly (child killed), not wait out the full 10s sleep.
    assert elapsed < 5.0
