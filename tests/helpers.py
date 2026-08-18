"""Builders for minimal, sanitized Harbor job/trial directory fixtures.

These reproduce the on-disk *shape* Harbor produces (job config.json/result.json,
trial config.json/result.json/lock.json, verifier/details.json, an
agent/trajectory.json with a couple of steps) without depending on any real
job under the repository's ``jobs/`` directory, and without any real model
output, API keys, or customer data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_TASK_NAME = "artifact-vault-recovery"
DEFAULT_TASK_VERSION = "0.1.0"
DEFAULT_TASK_DIGEST = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
DEFAULT_TASK_PATH = "benchmark/sample-tasks/artifact-vault-recovery"
DEFAULT_MODEL_NAME = "gpt-5.6-terra"
DEFAULT_MODEL_PROVIDER = "openai"
DEFAULT_AGENT_NAME = "terminus-2"
DEFAULT_AGENT_VERSION = "2.0.0"
DEFAULT_AGENT_KWARGS = {"reasoning_effort": "medium", "use_responses_api": True, "max_turns": 30}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def write_trial(
    job_dir: Path,
    trial_dir_name: str,
    *,
    trial_name: str | None = None,
    trial_uuid: str = "00000000-0000-4000-8000-000000000000",
    started_at: str = "2026-01-01T00:00:00.000000Z",
    finished_at: str = "2026-01-01T00:01:00.000000Z",
    task_name: str = DEFAULT_TASK_NAME,
    task_version: str | None = DEFAULT_TASK_VERSION,
    task_digest: str | None = DEFAULT_TASK_DIGEST,
    task_path: str = DEFAULT_TASK_PATH,
    include_lock: bool = True,
    model_name: str = DEFAULT_MODEL_NAME,
    model_provider: str | None = DEFAULT_MODEL_PROVIDER,
    agent_name: str = DEFAULT_AGENT_NAME,
    agent_version: str | None = DEFAULT_AGENT_VERSION,
    agent_kwargs: dict[str, Any] | None = None,
    rewards: dict[str, float] | None = None,
    checks: dict[str, bool] | None = None,
    cost_usd: float | None = 0.1,
    input_tokens: int | None = 1000,
    output_tokens: int | None = 100,
    cached_tokens: int | None = 0,
    exception: dict[str, str] | None = None,
    extra_agent_result: dict[str, Any] | None = None,
    n_steps: int = 2,
    include_trajectory: bool = True,
) -> Path:
    """Write one minimal, sanitized trial directory under ``job_dir``."""
    trial_dir = job_dir / trial_dir_name
    trial_name = trial_name or f"{task_name}__{trial_dir_name}"
    agent_kwargs = DEFAULT_AGENT_KWARGS if agent_kwargs is None else agent_kwargs
    full_model_name = f"{model_provider}/{model_name}" if model_provider else model_name

    _write_json(
        trial_dir / "config.json",
        {
            "task": {"path": task_path},
            "trial_name": trial_name,
            "trials_dir": str(job_dir),
            "agent": {"name": agent_name, "model_name": full_model_name, "kwargs": agent_kwargs},
            "job_id": "job-" + trial_dir_name,
        },
    )

    if include_lock:
        _write_json(
            trial_dir / "lock.json",
            {
                "schema_version": 2,
                "task": {
                    "name": task_name,
                    "version": task_version,
                    "type": "local",
                    "digest": task_digest,
                    "path": task_path,
                },
            },
        )

    agent_result: dict[str, Any] | None = None
    if not exception:
        agent_result = {
            "n_input_tokens": input_tokens,
            "n_cache_tokens": cached_tokens,
            "n_output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "rollout_details": [],
            "metadata": {"n_episodes": n_steps},
        }
        if extra_agent_result:
            agent_result.update(extra_agent_result)

    verifier_result = None if exception else {"rewards": rewards or {}}

    result: dict[str, Any] = {
        "id": trial_uuid,
        "task_name": task_name,
        "trial_name": trial_name,
        "task_id": {"path": task_path},
        "agent_info": None
        if exception
        else {
            "name": agent_name,
            "version": agent_version,
            "model_info": {"name": model_name, "provider": model_provider},
        },
        "agent_result": agent_result,
        "verifier_result": verifier_result,
        "exception_info": exception,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    _write_json(trial_dir / "result.json", result)

    if checks is not None:
        details = {
            name: {"passed": passed, "value": passed} for name, passed in checks.items()
        }
        _write_json(trial_dir / "verifier" / "details.json", details)
        _write_json(trial_dir / "verifier" / "reward.json", rewards or {})

    if include_trajectory:
        _write_json(
            trial_dir / "agent" / "trajectory.json",
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "session-" + trial_dir_name,
                "agent": {"name": agent_name, "version": agent_version, "model_name": full_model_name},
                "final_metrics": {
                    "total_prompt_tokens": input_tokens,
                    "total_completion_tokens": output_tokens,
                    "total_cached_tokens": cached_tokens,
                    "total_cost_usd": cost_usd,
                },
                "steps": [{"step_id": i, "message": "sanitized"} for i in range(n_steps)],
            },
        )

    return trial_dir


def write_job(
    tmp_path: Path,
    job_name: str,
    *,
    job_uuid: str = "00000000-0000-4000-8000-000000000001",
    started_at: str = "2026-01-01T00:00:00.000000",
    finished_at: str = "2026-01-01T00:01:00.000000",
    n_total_trials: int = 1,
    n_completed_trials: int = 1,
    n_errored_trials: int = 0,
    model_name: str = DEFAULT_MODEL_NAME,
    model_provider: str | None = DEFAULT_MODEL_PROVIDER,
    agent_name: str = DEFAULT_AGENT_NAME,
    agent_kwargs: dict[str, Any] | None = None,
    trials: list[dict[str, Any]] | None = None,
) -> Path:
    """Materialize a minimal, sanitized Harbor job directory under ``tmp_path``.

    ``trials`` is a list of kwargs dicts, each forwarded to :func:`write_trial`
    (minus ``job_dir``); a trial dir name defaults to ``trial_0``, ``trial_1``, ...
    """
    job_dir = tmp_path / job_name
    full_model_name = f"{model_provider}/{model_name}" if model_provider else model_name

    _write_json(
        job_dir / "config.json",
        {
            "agents": [{"name": agent_name, "model_name": full_model_name, "kwargs": agent_kwargs or DEFAULT_AGENT_KWARGS}],
            "tasks": [{"path": DEFAULT_TASK_PATH}],
        },
    )

    trials = trials if trials is not None else [{}]
    _write_json(
        job_dir / "result.json",
        {
            "id": job_uuid,
            "started_at": started_at,
            "finished_at": finished_at,
            "n_total_trials": n_total_trials,
            "stats": {
                "n_completed_trials": n_completed_trials,
                "n_errored_trials": n_errored_trials,
                "n_running_trials": 0,
                "n_pending_trials": 0,
                "n_cancelled_trials": 0,
            },
        },
    )
    (job_dir / "job.log").write_text("sanitized fixture job log\n")

    for index, trial_kwargs in enumerate(trials):
        merged = {
            "model_name": model_name,
            "model_provider": model_provider,
            "agent_name": agent_name,
        }
        merged.update(trial_kwargs)
        dir_name = merged.pop("trial_dir_name", f"trial_{index}")
        write_trial(job_dir, dir_name, **merged)

    return job_dir
