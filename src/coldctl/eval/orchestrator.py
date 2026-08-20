"""Run/resume orchestration: ties config, planner, manifest, Harbor
invocation, retry classification, Phase 1 ingestion, and report generation
together into one safe, resumable loop.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from coldctl.eval import manifest as manifest_module
from coldctl.eval.classifier import Classification, TrialOutcome, classify_missing_result, classify_trial_result
from coldctl.eval.config import EvaluationConfig, config_to_dict
from coldctl.eval.harbor_runner import HarborRunner, ProgressUpdate
from coldctl.eval.manifest import RunState, TrialState
from coldctl.eval.planner import Plan, TrialSpec, build_plan, compute_config_hash, hash_tasks
from coldctl.eval.progress import ProgressEvent
from coldctl.eval.redact import redact_text

ProgressCallback = Callable[[ProgressEvent], None]

_VERIFYING_SIGNAL_RE = re.compile(r"verif", re.IGNORECASE)
from coldctl.results import db as db_module
from coldctl.results.aggregate import EmptyTrialSelectionError, UnknownTrialKeysError
from coldctl.results.ingest import ingest_jobs
from coldctl.results.reports import build_report, render_json


class ReportMembershipError(Exception):
    """Raised when a run's recorded trial-key membership cannot be resolved
    against the Phase 1 store (e.g. stale/unknown trial keys)."""


class PreflightError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class BudgetExceededError(Exception):
    pass


class BudgetAcknowledgmentRequiredError(Exception):
    pass


class ResumeDriftError(Exception):
    pass


class RunInterrupted(Exception):
    def __init__(self, state: RunState) -> None:
        super().__init__("run interrupted and marked paused")
        self.state = state


@dataclass
class RunOutcome:
    run_id: str
    run_dir: Path
    state: RunState


def preflight_environment_checks(config: EvaluationConfig, *, check_docker: bool = True) -> list[str]:
    """Zero-cost, read-only checks. Reports only presence/absence of secrets,
    never their values."""
    problems: list[str] = []
    for tool in ("git", "harbor"):
        if shutil.which(tool) is None:
            problems.append(f"required command not found on PATH: {tool}")
    needs_docker = any(s.environment == "docker" for s in config.systems)
    if check_docker and needs_docker and shutil.which("docker") is None:
        problems.append("environment type 'docker' is used but the 'docker' command was not found")

    for system in config.systems:
        if not os.environ.get(system.api_key_env):
            problems.append(f"required environment variable is not set: {system.api_key_env}")
    return problems


def _run_id_for(config: EvaluationConfig) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{config.id}__{stamp}"


def _build_manifest_dict(config: EvaluationConfig, plan: Plan, *, run_id: str) -> dict:
    distinct_systems = {}
    for trial in plan.trials:
        distinct_systems.setdefault(
            trial.system_key,
            {
                "system_key": trial.system_key,
                "provider": trial.provider,
                "model": trial.model,
                "agent": trial.agent,
                "environment": trial.environment,
                "agent_kwargs": dict(trial.agent_kwargs),
                "api_key_env": trial.api_key_env,
            },
        )
    return {
        "schema_version": manifest_module.MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "config_path": str(config.source_path) if config.source_path else None,
        "config_hash": plan.config_hash,
        "config_snapshot": config_to_dict(config),
        "git_commit": plan.git_commit,
        "git_dirty": plan.git_dirty,
        "git_available": plan.git_available,
        "unverified": plan.unverified,
        "benchmark_version": plan.benchmark_version,
        "task_hashes": dict(plan.task_hashes),
        "trials": [t.to_dict() for t in plan.trials],
        "systems": list(distinct_systems.values()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expected_trial_count": len(plan.trials),
        "configured_budget_usd": config.execution.max_budget_usd,
        "cost_estimate": plan.cost_estimate.to_dict(),
        "reports": {
            "private": {"enabled": config.reports.private.enabled, "path": config.reports.private.path},
            "public": {"enabled": config.reports.public.enabled, "path": config.reports.public.path},
        },
    }


def create_run(
    config: EvaluationConfig,
    plan: Plan,
    *,
    coldstart_dir: Path,
    run_id: str | None = None,
) -> tuple[str, Path, dict, RunState]:
    """Freeze the manifest for an already-built plan and write the initial
    state. Does not execute any trial. Never overwrites an existing manifest."""
    run_id = run_id or _run_id_for(config)
    run_dir = manifest_module.run_dir_for(coldstart_dir, run_id)

    manifest_dict = _build_manifest_dict(config, plan, run_id=run_id)
    manifest_module.write_manifest(run_dir, manifest_dict)

    state = manifest_module.new_state(run_id, [t.trial_id for t in plan.trials])
    manifest_module.write_state(run_dir, state)
    manifest_module.append_event(
        manifest_module.events_path(run_dir),
        {
            "event_type": "run_created",
            "run_id": run_id,
            "trial_count": len(plan.trials),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    return run_id, run_dir, manifest_dict, state


def _trial_specs_from_manifest(manifest_dict: dict) -> dict[str, TrialSpec]:
    specs = {}
    for raw in manifest_dict["trials"]:
        specs[raw["trial_id"]] = TrialSpec(
            trial_id=raw["trial_id"],
            task_path=raw["task_path"],
            task_name=raw["task_name"],
            system_key=raw["system_key"],
            provider=raw["provider"],
            model=raw["model"],
            agent=raw["agent"],
            environment=raw["environment"],
            agent_kwargs=dict(raw["agent_kwargs"]),
            api_key_env=raw["api_key_env"],
            attempt=raw["attempt"],
        )
    return specs


def verify_resume_compatibility(
    manifest_dict: dict, config: EvaluationConfig, *, repo_root: Path
) -> list[str]:
    """Returns a list of drift problems; empty means safe to resume."""
    problems: list[str] = []
    current_hash = compute_config_hash(config)
    if current_hash != manifest_dict["config_hash"]:
        problems.append(
            f"configuration has changed since this run was created "
            f"(was {manifest_dict['config_hash'][:12]}, now {current_hash[:12]})"
        )

    from coldctl.eval.git_info import get_git_info

    git_info = get_git_info(repo_root)
    if git_info.commit != manifest_dict.get("git_commit"):
        problems.append(
            f"git commit has changed since this run was created "
            f"(was {manifest_dict.get('git_commit')}, now {git_info.commit})"
        )

    current_task_hashes = hash_tasks(repo_root, config.tasks)
    for task_path, original_hash in manifest_dict["task_hashes"].items():
        current = current_task_hashes.get(task_path)
        if current != original_hash:
            problems.append(f"task contents changed since this run was created: {task_path}")
    return problems


def _classify_invocation(job_dir: Path, *, returncode: int, stderr: str) -> tuple[Classification, dict | None]:
    """Locate the trial's result.json under job_dir and classify it. Returns
    (classification, trial_result_dict_or_None)."""
    if not job_dir.is_dir() or not (job_dir / "result.json").is_file():
        return classify_missing_result(harbor_returncode=returncode, job_dir=str(job_dir), stderr=stderr), None

    trial_dirs = [
        p for p in job_dir.iterdir() if p.is_dir() and (p / "result.json").is_file() and (p / "config.json").is_file()
    ]
    if len(trial_dirs) != 1:
        return (
            classify_missing_result(
                harbor_returncode=returncode,
                job_dir=str(job_dir),
                stderr=f"expected exactly one trial directory, found {len(trial_dirs)}",
            ),
            None,
        )

    try:
        trial_result = json.loads((trial_dirs[0] / "result.json").read_text())
    except (OSError, ValueError) as exc:
        return (
            classify_missing_result(harbor_returncode=returncode, job_dir=str(job_dir), stderr=str(exc)),
            None,
        )
    return classify_trial_result(trial_result), trial_result


@dataclass
class ReportOutcome:
    generated: bool
    paths: list[str]


@dataclass
class RegenerateReportsOutcome:
    run_id: str
    private_report: manifest_module.ReportStatus
    public_report: manifest_module.ReportStatus


def _derive_phase1_trial_key(harbor_job_dir: Path) -> str | None:
    """Recompute a Phase 1 trial_key for an already-ingested Harbor job the
    same way ingestion itself derives it (run_key::trial_name). Used only to
    backfill runs created before ``phase1_trial_key`` was persisted
    directly in trial state; new runs capture it at ingestion time instead
    of relying on this."""
    job_dir = Path(harbor_job_dir)
    if not job_dir.is_dir():
        return None
    trial_dirs = [
        p
        for p in job_dir.iterdir()
        if p.is_dir() and (p / "result.json").is_file() and (p / "config.json").is_file()
    ]
    if len(trial_dirs) != 1:
        return None
    try:
        trial_result = json.loads((trial_dirs[0] / "result.json").read_text())
    except (OSError, ValueError):
        return None
    trial_name = trial_result.get("trial_name") or trial_dirs[0].name
    return f"{job_dir.name}::{trial_name}"


def _resolve_trial_key(trial_state: TrialState, *, repo_root: Path) -> str | None:
    if trial_state.phase1_trial_key:
        return trial_state.phase1_trial_key
    if trial_state.ingested and trial_state.harbor_job_dirs:
        job_path = Path(trial_state.harbor_job_dirs[-1])
        if not job_path.is_absolute():
            job_path = repo_root / job_path
        return _derive_phase1_trial_key(job_path)
    return None


def collect_trial_keys_by_pair(
    manifest_dict: dict,
    state: RunState,
    *,
    repo_root: Path,
    backfill_run_dir: Path | None = None,
) -> dict[tuple[str, str], list[str]]:
    """Group this run's ingested trials' Phase 1 trial_keys by
    (task_name, system_key), in trial-plan order -- the durable association
    between orchestration run, planned trial, Harbor job, and ingested
    Phase 1 trial that automatic report generation is scoped to.

    Only trials that reached a scored terminal state (passed/failed) and
    were successfully ingested contribute. If ``backfill_run_dir`` is given,
    any trial missing a persisted ``phase1_trial_key`` has one derived from
    the Harbor job directory already on disk and is written back atomically
    to state.json -- self-healing for runs created before this field
    existed, without ever needing to re-ingest or guess from task/system
    identity.
    """
    specs = _trial_specs_from_manifest(manifest_dict)
    grouped: dict[tuple[str, str], list[str]] = {}
    backfilled = False
    for trial_id, trial_state in state.trials.items():
        if trial_state.status not in ("passed", "failed") or not trial_state.ingested:
            continue
        key = trial_state.phase1_trial_key
        if key is None:
            key = _resolve_trial_key(trial_state, repo_root=repo_root)
            if key is not None and backfill_run_dir is not None:
                trial_state.phase1_trial_key = key
                backfilled = True
        if key is None:
            continue
        spec = specs[trial_id]
        grouped.setdefault((spec.task_name, spec.system_key), []).append(key)
    if backfilled and backfill_run_dir is not None:
        manifest_module.write_state(backfill_run_dir, state)
    return grouped


def generate_reports_for_run(
    *,
    run_id: str,
    trial_keys_by_pair: dict[tuple[str, str], list[str]],
    reports_config: dict,
    repo_root: Path,
    phase1_db_path: Path,
) -> tuple[ReportOutcome, ReportOutcome]:
    """Generate Phase 1 reports scoped to exactly this run's ingested
    trials -- one report per (task, system) pair present in the run. Never
    falls back to all-history aggregation: a pair with no ingested trials
    is simply skipped (nothing to report), and a recorded trial key that no
    longer resolves in the results store raises ``ReportMembershipError``
    rather than silently omitting or over-including data.
    """
    private_paths: list[str] = []
    public_paths: list[str] = []
    if not phase1_db_path.is_file():
        return ReportOutcome(False, []), ReportOutcome(False, [])

    from coldctl.eval.config import DEFAULT_PRIVATE_REPORTS_PATH, DEFAULT_PUBLIC_REPORTS_PATH

    private_cfg = reports_config.get("private") or {}
    public_cfg = reports_config.get("public") or {}
    private_enabled = private_cfg.get("enabled", True)
    public_enabled = public_cfg.get("enabled", True)
    private_root = private_cfg.get("path") or DEFAULT_PRIVATE_REPORTS_PATH
    public_root = public_cfg.get("path") or DEFAULT_PUBLIC_REPORTS_PATH

    conn = db_module.connect(phase1_db_path)
    try:
        for task_name, system_key in sorted(trial_keys_by_pair.keys()):
            trial_keys = trial_keys_by_pair[(task_name, system_key)]
            try:
                if private_enabled:
                    private_report = build_report(
                        conn,
                        task=task_name,
                        system=system_key,
                        visibility="private",
                        trial_keys=trial_keys,
                        orchestration_run_id=run_id,
                    )
                    private_dir = repo_root / private_root / run_id
                    private_dir.mkdir(parents=True, exist_ok=True)
                    private_file = private_dir / f"{task_name}__{system_key}.private.json"
                    private_file.write_text(render_json(private_report))
                    private_paths.append(str(private_file.relative_to(repo_root)))

                if public_enabled:
                    public_report = build_report(
                        conn,
                        task=task_name,
                        system=system_key,
                        visibility="public",
                        trial_keys=trial_keys,
                        orchestration_run_id=run_id,
                    )
                    public_dir = repo_root / public_root / run_id
                    public_dir.mkdir(parents=True, exist_ok=True)
                    public_file = public_dir / f"{task_name}__{system_key}.public.json"
                    public_file.write_text(render_json(public_report))
                    public_paths.append(str(public_file.relative_to(repo_root)))
            except EmptyTrialSelectionError:
                continue  # nothing ingested for this pair; nothing to report
            except UnknownTrialKeysError as exc:
                raise ReportMembershipError(
                    f"run {run_id}: recorded trial key(s) for {task_name}/{system_key} "
                    f"not found in the results store: {exc.missing_keys}"
                ) from exc
    finally:
        conn.close()

    return (
        ReportOutcome(bool(private_paths), private_paths),
        ReportOutcome(bool(public_paths), public_paths),
    )


def regenerate_reports(
    run_id: str, *, repo_root: Path, coldstart_dir: Path, phase1_db_path: Path
) -> RegenerateReportsOutcome:
    """Regenerate Phase 1 reports for a completed orchestration run without
    executing Harbor. Reuses the run's frozen manifest and persisted
    trial-key membership (backfilling it from disk for runs created before
    that was tracked directly). Idempotent: safe to call repeatedly -- the
    membership is stable once recorded, so repeated calls reproduce the
    same figures (only the ``generated_at`` timestamp changes)."""
    run_dir = manifest_module.run_dir_for(coldstart_dir, run_id)
    manifest_dict = manifest_module.read_manifest(run_dir)
    state = manifest_module.read_state(run_dir)

    trial_keys_by_pair = collect_trial_keys_by_pair(
        manifest_dict, state, repo_root=repo_root, backfill_run_dir=run_dir
    )
    private_outcome, public_outcome = generate_reports_for_run(
        run_id=run_id,
        trial_keys_by_pair=trial_keys_by_pair,
        reports_config=manifest_dict.get("reports") or {},
        repo_root=repo_root,
        phase1_db_path=phase1_db_path,
    )
    state.private_report = manifest_module.ReportStatus(
        generated=private_outcome.generated, path=", ".join(private_outcome.paths) or None
    )
    state.public_report = manifest_module.ReportStatus(
        generated=public_outcome.generated, path=", ".join(public_outcome.paths) or None
    )
    manifest_module.write_state(run_dir, state)
    manifest_module.append_event(
        manifest_module.events_path(run_dir),
        {
            "event_type": "reports_regenerated",
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    return RegenerateReportsOutcome(
        run_id=run_id, private_report=state.private_report, public_report=state.public_report
    )


def _write_trial_log(run_dir: Path, job_name: str, result) -> None:
    """Persist Harbor's stdout/stderr for this attempt under run_dir/logs/,
    redacted the same way command arguments are -- this is the one place a
    misbehaving upstream client library could theoretically have echoed a
    credential, so it is sanitized before ever touching disk here."""
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f"argv: {result.argv_redacted}\n"
        f"returncode: {result.returncode}\n"
        f"--- stdout ---\n{redact_text(result.stdout)}\n"
        f"--- stderr ---\n{redact_text(result.stderr)}\n"
    )
    (logs_dir / f"{job_name}.log").write_text(content)


def execute_loop(
    run_dir: Path,
    manifest_dict: dict,
    state: RunState,
    config: EvaluationConfig,
    *,
    harbor_runner: HarborRunner,
    repo_root: Path,
    phase1_db_path: Path,
    progress_callback: ProgressCallback | None = None,
) -> RunState:
    """Execute all pending trials to completion, or until interrupted, an
    unrecoverable/unknown outcome pauses the run, or the budget is reached.

    ``progress_callback``, if given, is invoked with a :class:`ProgressEvent`
    at every meaningful phase transition (including live ticks while a
    Harbor invocation is in flight) so a caller can render visible progress.
    Overall completion percentage on each event is always derived from
    actual completed/planned trial counts, never elapsed time.
    """
    specs = _trial_specs_from_manifest(manifest_dict)
    trial_order = list(specs.keys())
    events = manifest_module.events_path(run_dir)
    jobs_dir = run_dir / "harbor_jobs"
    max_infra_retries = config.execution.max_infra_retries
    max_budget = config.execution.max_budget_usd

    # A trial left "running" from a prior, interrupted process cannot be
    # trusted; treat it as not-yet-attempted-this-time.
    for trial in state.trials.values():
        if trial.status == "running":
            trial.status = "pending"
    state.status = "running"
    manifest_module.write_state(run_dir, state)

    stop_reason: str | None = None
    try:
        while True:
            pending = state.pending_trial_ids
            if not pending:
                break
            if state.actual_cost_usd >= max_budget:
                stop_reason = "budget_reached"
                break

            trial_id = pending[0]
            trial_state = state.trials[trial_id]
            spec = specs[trial_id]
            attempt_number = trial_state.attempts + 1
            job_name = f"{trial_id}--try{attempt_number:02d}"
            trial_number = trial_order.index(trial_id) + 1
            planned_total = len(trial_order)

            def _emit(
                phase: str,
                *,
                elapsed_sec: float = 0.0,
                harbor_status: str = "not_started",
                stdout_tail: str = "",
            ) -> None:
                if progress_callback is None:
                    return
                progress_callback(
                    ProgressEvent(
                        run_id=state.run_id,
                        completed_trials=len(state.completed_trial_ids),
                        planned_trials=planned_total,
                        current_trial_number=trial_number,
                        task_name=spec.task_name,
                        model=spec.model,
                        agent=spec.agent,
                        phase=phase,
                        elapsed_sec=elapsed_sec,
                        harbor_status=harbor_status,
                        stdout_tail=stdout_tail,
                    )
                )

            _emit("preparing")

            trial_state.status = "running"
            trial_state.last_updated = datetime.now(timezone.utc).isoformat()
            manifest_module.write_state(run_dir, state)
            manifest_module.append_event(
                events,
                {
                    "event_type": "trial_started",
                    "run_id": state.run_id,
                    "trial_id": trial_id,
                    "attempt": attempt_number,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )

            _emit("launching_harbor")

            def _on_harbor_progress(update: ProgressUpdate) -> None:
                phase = "verifying" if _VERIFYING_SIGNAL_RE.search(update.stdout_tail) else "agent_running"
                _emit(
                    phase,
                    elapsed_sec=update.elapsed_sec,
                    harbor_status="active" if update.harbor_alive else "exited",
                    stdout_tail=update.stdout_tail,
                )

            result = harbor_runner.run_trial(
                trial=spec,
                job_name=job_name,
                jobs_dir=jobs_dir,
                on_progress=_on_harbor_progress if progress_callback is not None else None,
            )
            classification, trial_result = _classify_invocation(
                result.job_dir, returncode=result.returncode, stderr=result.stderr
            )
            _write_trial_log(run_dir, job_name, result)

            trial_state.attempts = attempt_number
            trial_state.harbor_job_dirs.append(str(result.job_dir))
            trial_state.outcome_reason = classification.reason
            trial_state.evidence = classification.evidence
            trial_state.last_updated = datetime.now(timezone.utc).isoformat()

            cost = None
            if trial_result is not None:
                cost = ((trial_result.get("agent_result") or {}).get("cost_usd"))
            if cost is not None:
                trial_state.cost_usd = cost
                state.actual_cost_usd += cost

            event: dict = {
                "run_id": state.run_id,
                "trial_id": trial_id,
                "attempt": attempt_number,
                "outcome": classification.outcome.value,
                "reason": classification.reason,
                "evidence": classification.evidence,
                "harbor_argv": result.argv_redacted,
                "harbor_job_dir": str(result.job_dir),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            if classification.outcome in (TrialOutcome.PASSED, TrialOutcome.FAILED):
                trial_state.status = classification.outcome.value
                _emit("ingesting", harbor_status="exited")
                conn = db_module.connect(phase1_db_path)
                try:
                    ingest_results = ingest_jobs(conn, [result.job_dir])
                finally:
                    conn.close()
                ingest_ok = bool(ingest_results and ingest_results[0].ok)
                trial_state.ingested = ingest_ok
                # Capture the exact, normalized Phase 1 identifier this trial
                # ingested to, durably, so report generation can later be
                # scoped to precisely this run's trials -- never inferred
                # from task/system identity alone.
                if ingest_ok and ingest_results[0].trials:
                    trial_state.phase1_trial_key = ingest_results[0].trials[0].trial_key
                event["event_type"] = "trial_completed" if classification.outcome == TrialOutcome.PASSED else "trial_failed"
            elif classification.outcome == TrialOutcome.INFRA_INVALID:
                state.invalid_infrastructure_attempts += 1
                if attempt_number < max_infra_retries + 1:
                    trial_state.status = "infra_invalid_retry_scheduled"
                    event["event_type"] = "trial_infra_retry"
                else:
                    trial_state.status = "infra_invalid_exhausted"
                    event["event_type"] = "trial_infra_exhausted"
            elif classification.outcome == TrialOutcome.AUTH_ERROR:
                trial_state.status = "auth_error_paused"
                event["event_type"] = "trial_auth_error"
                stop_reason = "authentication_failure"
            else:
                trial_state.status = "unknown_paused"
                event["event_type"] = "trial_unknown_paused"
                stop_reason = "unknown_exception"

            manifest_module.write_state(run_dir, state)
            manifest_module.append_event(events, event)
            state.last_event = event
            _emit("completed", harbor_status="exited")

            if stop_reason:
                break
    except KeyboardInterrupt:
        stop_reason = "interrupted"

    if stop_reason:
        state.status = "paused"
        manifest_module.write_state(run_dir, state)
        manifest_module.append_event(
            events,
            {
                "event_type": "run_paused",
                "run_id": state.run_id,
                "reason": stop_reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        if stop_reason == "interrupted":
            raise RunInterrupted(state)
        return state

    any_exhausted = any(t.status == "infra_invalid_exhausted" for t in state.trials.values())
    state.status = "failed" if any_exhausted else "completed"
    manifest_module.write_state(run_dir, state)

    if progress_callback is not None:
        planned_total = len(trial_order)
        progress_callback(
            ProgressEvent(
                run_id=state.run_id,
                completed_trials=len(state.completed_trial_ids),
                planned_trials=planned_total,
                current_trial_number=planned_total,
                task_name="",
                model="",
                agent="",
                phase="generating_reports",
                elapsed_sec=0.0,
                harbor_status="exited",
            )
        )

    trial_keys_by_pair = collect_trial_keys_by_pair(
        manifest_dict, state, repo_root=repo_root, backfill_run_dir=run_dir
    )
    private_outcome, public_outcome = generate_reports_for_run(
        run_id=state.run_id,
        trial_keys_by_pair=trial_keys_by_pair,
        reports_config=manifest_dict.get("reports") or {},
        repo_root=repo_root,
        phase1_db_path=phase1_db_path,
    )
    state.private_report = manifest_module.ReportStatus(
        generated=private_outcome.generated, path=", ".join(private_outcome.paths) or None
    )
    state.public_report = manifest_module.ReportStatus(
        generated=public_outcome.generated, path=", ".join(public_outcome.paths) or None
    )
    manifest_module.write_state(run_dir, state)
    manifest_module.append_event(
        events,
        {
            "event_type": "run_finished",
            "run_id": state.run_id,
            "status": state.status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    if progress_callback is not None:
        planned_total = len(trial_order)
        progress_callback(
            ProgressEvent(
                run_id=state.run_id,
                completed_trials=len(state.completed_trial_ids),
                planned_trials=planned_total,
                current_trial_number=planned_total,
                task_name="",
                model="",
                agent="",
                phase="completed",
                elapsed_sec=0.0,
                harbor_status="exited",
            )
        )
    return state


def check_budget(plan: Plan, config: EvaluationConfig, *, budget_ack: bool) -> None:
    """Refuse to spend money the estimate says we can't afford, and refuse to
    proceed on an unestimated budget unless explicitly acknowledged."""
    estimate = plan.cost_estimate.total_usd
    if estimate is not None:
        if estimate > config.execution.max_budget_usd:
            raise BudgetExceededError(
                f"estimated cost ${estimate:.4f} ({plan.cost_estimate.source}) exceeds "
                f"configured max_budget_usd ${config.execution.max_budget_usd:.4f}"
            )
        return
    if not budget_ack:
        raise BudgetAcknowledgmentRequiredError(
            "no cost estimate is available (no historical data and no estimated_cost_per_trial_usd "
            "configured); pass an explicit budget acknowledgment to proceed at your own risk"
        )


def run_evaluation(
    config: EvaluationConfig,
    *,
    repo_root: Path,
    coldstart_dir: Path,
    phase1_db_path: Path,
    allow_dirty: bool,
    budget_ack: bool,
    harbor_runner: HarborRunner,
    run_id: str | None = None,
    skip_preflight: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> RunOutcome:
    """Full fresh-run pipeline: preflight -> plan -> budget gate -> freeze
    manifest -> execute. Assumes the caller has already obtained --yes.

    ``skip_preflight`` exists only for tests that inject a fake Harbor
    runner: real command-existence and credential-presence checks are
    meaningless when nothing will actually shell out to the real `harbor`
    binary or a real model provider. The real CLI (`coldctl eval run`) never
    sets this."""
    if not skip_preflight:
        problems = preflight_environment_checks(config)
        if problems:
            raise PreflightError(problems)

    plan = build_plan(config, repo_root=repo_root, phase1_db_path=phase1_db_path, allow_dirty=allow_dirty)
    check_budget(plan, config, budget_ack=budget_ack)

    run_id, run_dir, manifest_dict, state = create_run(
        config, plan, coldstart_dir=coldstart_dir, run_id=run_id
    )
    final_state = execute_loop(
        run_dir,
        manifest_dict,
        state,
        config,
        harbor_runner=harbor_runner,
        repo_root=repo_root,
        phase1_db_path=phase1_db_path,
        progress_callback=progress_callback,
    )
    return RunOutcome(run_id=run_id, run_dir=run_dir, state=final_state)


def resume_evaluation(
    run_id: str,
    config: EvaluationConfig,
    *,
    repo_root: Path,
    coldstart_dir: Path,
    phase1_db_path: Path,
    budget_ack: bool,
    harbor_runner: HarborRunner,
    skip_preflight: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> RunOutcome:
    """Resume an existing run: reload its frozen manifest (never regenerated),
    verify nothing relevant has drifted, and continue only pending/
    infra-retry-eligible trials. Idempotent: calling this on an
    already-completed/failed run is a safe no-op. See ``run_evaluation`` for
    ``skip_preflight``."""
    run_dir = manifest_module.run_dir_for(coldstart_dir, run_id)
    manifest_dict = manifest_module.read_manifest(run_dir)
    state = manifest_module.read_state(run_dir)

    if state.status in ("completed", "failed"):
        return RunOutcome(run_id=run_id, run_dir=run_dir, state=state)

    drift = verify_resume_compatibility(manifest_dict, config, repo_root=repo_root)
    if drift:
        raise ResumeDriftError("cannot resume: " + "; ".join(drift))

    if not skip_preflight:
        problems = preflight_environment_checks(config)
        if problems:
            raise PreflightError(problems)

    estimate = manifest_dict["cost_estimate"]["total_usd"]
    remaining_budget = config.execution.max_budget_usd - state.actual_cost_usd
    if estimate is not None and remaining_budget < 0:
        raise BudgetExceededError(
            f"accumulated cost ${state.actual_cost_usd:.4f} already exceeds "
            f"configured max_budget_usd ${config.execution.max_budget_usd:.4f}"
        )
    if estimate is None and not budget_ack:
        raise BudgetAcknowledgmentRequiredError(
            "no cost estimate was available when this run was created; pass an explicit "
            "budget acknowledgment to resume"
        )

    final_state = execute_loop(
        run_dir,
        manifest_dict,
        state,
        config,
        harbor_runner=harbor_runner,
        repo_root=repo_root,
        phase1_db_path=phase1_db_path,
        progress_callback=progress_callback,
    )
    return RunOutcome(run_id=run_id, run_dir=run_dir, state=final_state)
