"""A fake, substitutable Harbor runner for tests.

Never invokes a real provider, API, Docker daemon, or Harbor evaluation.
Instead it writes a minimal, sanitized Harbor job directory (reusing Phase
1's own fixture builders, so ingestion exercises the exact same code path a
real job would) according to a scripted outcome queue keyed by trial ID.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from coldctl.eval.harbor_runner import HarborInvocationResult, ProgressUpdate
from coldctl.eval.planner import TrialSpec

from tests.helpers import write_job


@dataclass
class ScriptedOutcome:
    kind: str  # "passed" | "failed" | "infra_invalid" | "auth_error" | "unknown" | "missing_result"
    cost_usd: float = 0.10
    rewards: dict[str, float] | None = None
    checks: dict[str, bool] | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    returncode: int = 0
    stderr: str = ""
    #: Simulated wall-clock duration of the fake "Harbor process", in
    #: seconds. When > 0 and an ``on_progress`` callback is supplied,
    #: ``run_trial`` emits one ``ProgressUpdate`` per ``tick_sec`` via the
    #: injected ``sleep_fn`` -- no real delay is required in tests.
    duration_sec: float = 0.0
    tick_sec: float = 0.1
    stdout_during_run: str = ""


def _default_rewards(kind: str) -> dict[str, float]:
    if kind == "passed":
        return {
            "coldstart_pass": 1.0,
            "functional": 1.0,
            "durability": 1.0,
            "state_safety": 1.0,
            "integrity": 1.0,
            "evidence": 1.0,
        }
    return {
        "coldstart_pass": 0.0,
        "functional": 0.5,
        "durability": 0.0,
        "state_safety": 0.5,
        "integrity": 0.0,
        "evidence": 0.0,
    }


_EXCEPTION_DEFAULTS = {
    "auth_error": ("AuthenticationError", "Incorrect API key provided"),
    "infra_invalid": ("HealthcheckError", "environment healthcheck failed before the agent began"),
    "unknown": ("WeirdNewError", "an exception type nobody has seen before"),
}


class FakeHarborRunner:
    """Implements the ``HarborRunner`` protocol structurally (duck-typed).

    ``sleep_fn``/``clock_fn`` are overridable so progress-rendering tests can
    simulate a slow-running Harbor process with an injected clock/sleeper
    instead of real wall-clock delays.
    """

    def __init__(
        self,
        script: dict[str, list[ScriptedOutcome]],
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._script: dict[str, list[ScriptedOutcome]] = {k: list(v) for k, v in script.items()}
        self.calls: list[dict[str, Any]] = []
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn

    def run_trial(
        self,
        *,
        trial: TrialSpec,
        job_name: str,
        jobs_dir: Path,
        on_progress: Callable[[ProgressUpdate], None] | None = None,
    ) -> HarborInvocationResult:
        self.calls.append({"trial_id": trial.trial_id, "job_name": job_name, "jobs_dir": str(jobs_dir)})
        queue = self._script.get(trial.trial_id)
        if not queue:
            raise AssertionError(f"fake harbor runner has no scripted outcome left for {trial.trial_id}")
        outcome = queue.pop(0)
        job_dir = Path(jobs_dir) / job_name

        argv_redacted = ["harbor", "run", "-p", trial.task_path, "-a", trial.agent, "-m", trial.model, "(fake)"]

        if on_progress is not None and outcome.duration_sec > 0:
            start = self._clock_fn()
            elapsed = 0.0
            while elapsed < outcome.duration_sec:
                on_progress(
                    ProgressUpdate(
                        elapsed_sec=elapsed, harbor_alive=True, stdout_tail=outcome.stdout_during_run
                    )
                )
                self._sleep_fn(outcome.tick_sec)
                elapsed = self._clock_fn() - start
            on_progress(ProgressUpdate(elapsed_sec=elapsed, harbor_alive=False, stdout_tail=outcome.stdout_during_run))

        if outcome.kind == "missing_result":
            return HarborInvocationResult(
                returncode=outcome.returncode or 1,
                stdout="",
                stderr=outcome.stderr or "simulated harbor crash before writing a result",
                job_dir=job_dir,
                argv_redacted=argv_redacted,
            )

        provider, model_short = (trial.model.split("/", 1) if "/" in trial.model else (None, trial.model))

        if outcome.kind in ("passed", "failed"):
            write_job(
                Path(jobs_dir),
                job_name,
                model_name=model_short,
                model_provider=provider,
                agent_name=trial.agent,
                trials=[
                    {
                        "trial_dir_name": "trial_0",
                        "task_name": trial.task_name,
                        "task_path": trial.task_path,
                        "agent_kwargs": dict(trial.agent_kwargs),
                        "rewards": outcome.rewards or _default_rewards(outcome.kind),
                        "checks": outcome.checks or {"initial_readiness": outcome.kind == "passed"},
                        "cost_usd": outcome.cost_usd,
                    }
                ],
            )
        else:
            exc_type, exc_message = _EXCEPTION_DEFAULTS.get(outcome.kind, ("RuntimeError", "unspecified"))
            write_job(
                Path(jobs_dir),
                job_name,
                model_name=model_short,
                model_provider=provider,
                agent_name=trial.agent,
                trials=[
                    {
                        "trial_dir_name": "trial_0",
                        "task_name": trial.task_name,
                        "task_path": trial.task_path,
                        "agent_kwargs": dict(trial.agent_kwargs),
                        "exception": {
                            "exception_type": outcome.exception_type or exc_type,
                            "exception_message": outcome.exception_message or exc_message,
                            "exception_traceback": "Traceback (most recent call last): ...",
                            "occurred_at": "2026-01-01T00:00:00Z",
                        },
                    }
                ],
            )

        return HarborInvocationResult(
            returncode=outcome.returncode,
            stdout="",
            stderr=outcome.stderr,
            job_dir=job_dir,
            argv_redacted=argv_redacted,
        )
