"""Idempotent ingestion of completed Harbor jobs into the results store.

Only paths, hashes, and small structured metadata are read into SQLite.
Trajectory *contents* (the full step-by-step transcript) are never stored;
only a small metadata summary (schema version, agent, token/cost totals,
step count) is kept, alongside the trajectory file's path and SHA-256 hash
for provenance.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from coldctl import __version__ as COLDCTL_VERSION
from coldctl.results.constants import STRICT_PASS_KEY


class IngestError(Exception):
    """A job or trial could not be ingested. Carries enough context to be
    reported clearly to the operator without corrupting prior data."""


@dataclass
class TrialIngestResult:
    trial_key: str
    trial_name: str
    created: bool


@dataclass
class JobIngestResult:
    job_dir: str
    run_key: str
    ok: bool
    error: str | None = None
    trials: list[TrialIngestResult] = field(default_factory=list)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, *, context: str) -> Any:
    if not path.is_file():
        raise IngestError(f"{context}: missing required file {path}")
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestError(f"{context}: could not parse {path}: {exc}") from exc


def _try_read_json(path: Path) -> Any | None:
    """Best-effort read: returns None rather than raising for optional files."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _upsert(
    conn: sqlite3.Connection,
    table: str,
    *,
    conflict_columns: tuple[str, ...],
    values: dict[str, Any],
) -> int:
    """Insert-or-update `values` into `table`, returning the row id.

    Uses SQLite's ``ON CONFLICT ... DO UPDATE ... RETURNING`` so repeated
    ingestion of the same source data is idempotent: the same natural key
    always resolves to the same row instead of creating a duplicate.
    """
    columns = list(values.keys())
    placeholders = ", ".join(f":{c}" for c in columns)
    column_list = ", ".join(columns)
    update_columns = [c for c in columns if c not in conflict_columns]
    conflict_list = ", ".join(conflict_columns)
    if update_columns:
        update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_columns)
        conflict_clause = f"ON CONFLICT ({conflict_list}) DO UPDATE SET {update_clause}"
    else:
        conflict_clause = f"ON CONFLICT ({conflict_list}) DO NOTHING"
    sql = (
        f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
        f"{conflict_clause} RETURNING id"
    )
    row = conn.execute(sql, values).fetchone()
    if row is not None:
        return int(row["id"])
    # DO NOTHING path with no RETURNING row: fetch the existing id explicitly.
    where_clause = " AND ".join(f"{c} = :{c}" for c in conflict_columns)
    existing = conn.execute(
        f"SELECT id FROM {table} WHERE {where_clause}", values
    ).fetchone()
    if existing is None:  # pragma: no cover - defensive
        raise IngestError(f"upsert into {table} failed to produce a row id")
    return int(existing["id"])


def _upsert_benchmark_version(conn: sqlite3.Connection, version: str) -> int:
    return _upsert(
        conn,
        "benchmark_versions",
        conflict_columns=("version",),
        values={"version": version},
    )


def _upsert_task_version(
    conn: sqlite3.Connection,
    *,
    name: str,
    version: str | None,
    digest: str,
    path: str | None,
) -> int:
    task_id = _upsert(conn, "tasks", conflict_columns=("name",), values={"name": name})
    return _upsert(
        conn,
        "task_versions",
        conflict_columns=("task_id", "digest"),
        values={"task_id": task_id, "version": version, "digest": digest, "path": path},
    )


def _upsert_system(
    conn: sqlite3.Connection,
    *,
    model_name: str,
    model_provider: str | None,
    agent_name: str,
    agent_version: str | None,
    kwargs: dict[str, Any] | None,
) -> int:
    model_id = _upsert(
        conn,
        "models",
        conflict_columns=("name", "provider"),
        values={"name": model_name, "provider": model_provider},
    )
    agent_id = _upsert(
        conn,
        "agents",
        conflict_columns=("name", "version"),
        values={"name": agent_name, "version": agent_version},
    )
    system_key = f"{model_name}__{agent_name}"
    return _upsert(
        conn,
        "systems",
        conflict_columns=("system_key",),
        values={
            "system_key": system_key,
            "model_id": model_id,
            "agent_id": agent_id,
            "agent_kwargs_json": json.dumps(kwargs or {}, sort_keys=True),
        },
    )


def _record_artifact(
    conn: sqlite3.Connection,
    *,
    trial_id: int | None,
    run_id: int | None,
    kind: str,
    path: Path,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not path.is_file():
        return
    _upsert(
        conn,
        "artifact_references",
        conflict_columns=("kind", "source_path"),
        values={
            "trial_id": trial_id,
            "run_id": run_id,
            "kind": kind,
            "source_path": str(path.resolve()),
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
            "metadata_json": json.dumps(metadata) if metadata is not None else None,
        },
    )


def _find_trial_dirs(job_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in job_dir.iterdir()
        if p.is_dir() and (p / "result.json").is_file() and (p / "config.json").is_file()
    )


def _task_identity_from_lock(lock: dict[str, Any] | None, fallback_path: str | None) -> dict[str, Any]:
    if lock and isinstance(lock.get("task"), dict):
        task = lock["task"]
        name = task.get("name") or (fallback_path or "unknown-task").rsplit("/", 1)[-1]
        return {
            "name": name,
            "version": task.get("version"),
            "digest": task.get("digest") or f"unknown:{name}",
            "path": task.get("path") or fallback_path,
        }
    name = (fallback_path or "unknown-task").rsplit("/", 1)[-1]
    return {"name": name, "version": None, "digest": f"unknown:{name}", "path": fallback_path}


def _model_identity(
    agent_info: dict[str, Any] | None, config_agent: dict[str, Any]
) -> tuple[str, str | None, str, str | None]:
    """Returns (model_name, model_provider, agent_name, agent_version)."""
    if agent_info and isinstance(agent_info.get("model_info"), dict):
        model_info = agent_info["model_info"]
        model_name = model_info.get("name") or config_agent.get("model_name") or "unknown-model"
        model_provider = model_info.get("provider")
    else:
        raw_model = config_agent.get("model_name") or "unknown-model"
        if "/" in raw_model:
            model_provider, model_name = raw_model.split("/", 1)
        else:
            model_provider, model_name = None, raw_model
    agent_name = (agent_info or {}).get("name") or config_agent.get("name") or "unknown-agent"
    agent_version = (agent_info or {}).get("version")
    return model_name, model_provider, agent_name, agent_version


def _compute_runtime(
    *,
    run_started: str | None,
    run_finished: str | None,
    run_n_total_trials: int | None,
    trial_started: str | None,
    trial_finished: str | None,
) -> tuple[float | None, str | None]:
    """Pick the best available runtime measurement for a trial.

    When a job contains exactly one trial, the job's own started/finished
    envelope (matching what Harbor's CLI reports as "Total runtime") is used,
    since it captures dispatch/setup overhead that the trial's own internal
    clock does not. For multi-trial jobs, trials may run concurrently, so the
    job envelope does not correspond to any single trial's duration and the
    trial's own started_at/finished_at is used instead.
    """
    if run_n_total_trials == 1:
        start, finish = _parse_datetime(run_started), _parse_datetime(run_finished)
        if start is not None and finish is not None:
            return (finish - start).total_seconds(), "run"
    start, finish = _parse_datetime(trial_started), _parse_datetime(trial_finished)
    if start is not None and finish is not None:
        return (finish - start).total_seconds(), "trial"
    return None, None


def _ingest_trial(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    run_key: str,
    run_started_at: str | None,
    run_finished_at: str | None,
    run_n_total_trials: int | None,
    trial_dir: Path,
) -> TrialIngestResult:
    context = f"trial {trial_dir}"
    trial_config = _read_json(trial_dir / "config.json", context=context)
    trial_result = _read_json(trial_dir / "result.json", context=context)

    trial_name = trial_result.get("trial_name") or trial_config.get("trial_name") or trial_dir.name
    trial_key = f"{run_key}::{trial_name}"

    lock = _try_read_json(trial_dir / "lock.json")
    task_path = None
    if isinstance(trial_config.get("task"), dict):
        task_path = trial_config["task"].get("path")
    task_identity = _task_identity_from_lock(lock, task_path)
    task_version_id = _upsert_task_version(
        conn,
        name=task_identity["name"],
        version=task_identity["version"],
        digest=task_identity["digest"],
        path=task_identity["path"],
    )

    agent_info = trial_result.get("agent_info")
    config_agent = trial_config.get("agent") or {}
    model_name, model_provider, agent_name, agent_version = _model_identity(
        agent_info, config_agent
    )
    system_id = _upsert_system(
        conn,
        model_name=model_name,
        model_provider=model_provider,
        agent_name=agent_name,
        agent_version=agent_version,
        kwargs=config_agent.get("kwargs"),
    )

    exception_info = trial_result.get("exception_info")
    is_infra_exception = exception_info is not None

    verifier_result = trial_result.get("verifier_result") or {}
    rewards = verifier_result.get("rewards") or {}
    coldstart_pass_value = rewards.get(STRICT_PASS_KEY)
    strict_pass = None
    if coldstart_pass_value is not None:
        strict_pass = 1 if float(coldstart_pass_value) >= 1.0 else 0

    agent_result = trial_result.get("agent_result") or {}

    runtime_sec, runtime_basis = _compute_runtime(
        run_started=run_started_at,
        run_finished=run_finished_at,
        run_n_total_trials=run_n_total_trials,
        trial_started=trial_result.get("started_at"),
        trial_finished=trial_result.get("finished_at"),
    )

    trial_source_path = trial_dir / "result.json"
    values = {
        "run_id": run_id,
        "trial_key": trial_key,
        "trial_uuid": trial_result.get("id"),
        "trial_name": trial_name,
        "task_version_id": task_version_id,
        "system_id": system_id,
        "started_at": trial_result.get("started_at"),
        "finished_at": trial_result.get("finished_at"),
        "runtime_sec": runtime_sec,
        "runtime_basis": runtime_basis,
        "coldstart_pass": coldstart_pass_value,
        "strict_pass": strict_pass,
        "exception_type": (exception_info or {}).get("exception_type"),
        "exception_message": (exception_info or {}).get("exception_message"),
        "exception_traceback": (exception_info or {}).get("exception_traceback"),
        "exception_occurred_at": (exception_info or {}).get("occurred_at"),
        "is_infra_exception": int(is_infra_exception),
        "input_tokens": agent_result.get("n_input_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
        "cached_tokens": agent_result.get("n_cache_tokens"),
        "cost_usd": agent_result.get("cost_usd"),
        "agent_result_json": json.dumps(agent_result, sort_keys=True) if agent_result else None,
        "config_json": json.dumps(trial_config, sort_keys=True),
        "source_path": str(trial_source_path.resolve()),
        "source_sha256": _sha256_file(trial_source_path),
        "ingested_at": datetime.now().isoformat(),
    }
    trial_row = conn.execute(
        "SELECT id FROM trials WHERE trial_key = ?", (trial_key,)
    ).fetchone()
    created = trial_row is None
    trial_id = _upsert(conn, "trials", conflict_columns=("trial_key",), values=values)

    # Replace prior dimension scores / verifier checks for this trial so that
    # re-ingestion reflects the source of truth exactly (no stale leftovers).
    conn.execute("DELETE FROM dimension_scores WHERE trial_id = ?", (trial_id,))
    for dimension, value in rewards.items():
        conn.execute(
            "INSERT INTO dimension_scores (trial_id, dimension, value) VALUES (?, ?, ?)",
            (trial_id, dimension, value),
        )

    details = _try_read_json(trial_dir / "verifier" / "details.json") or {}
    conn.execute("DELETE FROM verifier_checks WHERE trial_id = ?", (trial_id,))
    for check_name, check_value in details.items():
        passed = check_value.get("passed") if isinstance(check_value, dict) else None
        conn.execute(
            "INSERT INTO verifier_checks (trial_id, check_name, passed, raw_json) "
            "VALUES (?, ?, ?, ?)",
            (
                trial_id,
                check_name,
                None if passed is None else int(bool(passed)),
                json.dumps(check_value),
            ),
        )

    _record_artifact(
        conn, trial_id=trial_id, run_id=None, kind="trial_result", path=trial_dir / "result.json"
    )
    _record_artifact(
        conn, trial_id=trial_id, run_id=None, kind="trial_config", path=trial_dir / "config.json"
    )
    _record_artifact(
        conn, trial_id=trial_id, run_id=None, kind="trial_lock", path=trial_dir / "lock.json"
    )
    _record_artifact(
        conn,
        trial_id=trial_id,
        run_id=None,
        kind="verifier_details",
        path=trial_dir / "verifier" / "details.json",
    )
    _record_artifact(
        conn,
        trial_id=trial_id,
        run_id=None,
        kind="verifier_reward",
        path=trial_dir / "verifier" / "reward.json",
    )
    _record_artifact(
        conn,
        trial_id=trial_id,
        run_id=None,
        kind="coldstart_report",
        path=trial_dir / "artifacts" / "app" / "coldstart-report.json",
    )

    trajectory_path = trial_dir / "agent" / "trajectory.json"
    if trajectory_path.is_file():
        trajectory = _try_read_json(trajectory_path)
        trajectory_meta = None
        if isinstance(trajectory, dict):
            trajectory_meta = {
                "schema_version": trajectory.get("schema_version"),
                "session_id": trajectory.get("session_id"),
                "agent": trajectory.get("agent"),
                "final_metrics": trajectory.get("final_metrics"),
                "n_steps": len(trajectory.get("steps") or []),
            }
        _record_artifact(
            conn,
            trial_id=trial_id,
            run_id=None,
            kind="trajectory",
            path=trajectory_path,
            metadata=trajectory_meta,
        )

    return TrialIngestResult(trial_key=trial_key, trial_name=trial_name, created=created)


def ingest_job(conn: sqlite3.Connection, job_dir: Path) -> JobIngestResult:
    """Ingest a single completed Harbor job directory. Raises IngestError on
    any malformed/missing required file; callers should run this inside a
    SAVEPOINT so a bad job cannot corrupt previously-ingested data."""
    job_dir = Path(job_dir)
    run_key = job_dir.name
    context = f"job {job_dir}"
    if not job_dir.is_dir():
        raise IngestError(f"{context}: not a directory")

    job_config = _read_json(job_dir / "config.json", context=context)
    job_result = _read_json(job_dir / "result.json", context=context)

    benchmark_version_id = _upsert_benchmark_version(conn, COLDCTL_VERSION)
    stats = job_result.get("stats") or {}
    run_id = _upsert(
        conn,
        "runs",
        conflict_columns=("run_key",),
        values={
            "run_key": run_key,
            "job_uuid": job_result.get("id"),
            "benchmark_version_id": benchmark_version_id,
            "started_at": job_result.get("started_at"),
            "finished_at": job_result.get("finished_at"),
            "n_total_trials": job_result.get("n_total_trials"),
            "n_completed_trials": stats.get("n_completed_trials"),
            "n_errored_trials": stats.get("n_errored_trials"),
            "config_json": json.dumps(job_config, sort_keys=True),
            "source_path": str(job_dir.resolve()),
            "source_sha256": _sha256_file(job_dir / "result.json"),
            "ingested_at": datetime.now().isoformat(),
        },
    )
    _record_artifact(conn, trial_id=None, run_id=run_id, kind="job_config", path=job_dir / "config.json")
    _record_artifact(conn, trial_id=None, run_id=run_id, kind="job_result", path=job_dir / "result.json")
    _record_artifact(conn, trial_id=None, run_id=run_id, kind="job_log", path=job_dir / "job.log")
    _record_artifact(conn, trial_id=None, run_id=run_id, kind="job_lock", path=job_dir / "lock.json")

    trial_dirs = _find_trial_dirs(job_dir)
    if not trial_dirs:
        raise IngestError(f"{context}: no trial directories found (expected subdirectories with config.json and result.json)")

    trial_results = [
        _ingest_trial(
            conn,
            run_id=run_id,
            run_key=run_key,
            run_started_at=job_result.get("started_at"),
            run_finished_at=job_result.get("finished_at"),
            run_n_total_trials=job_result.get("n_total_trials"),
            trial_dir=trial_dir,
        )
        for trial_dir in trial_dirs
    ]
    return JobIngestResult(job_dir=str(job_dir), run_key=run_key, ok=True, trials=trial_results)


def ingest_jobs(conn: sqlite3.Connection, job_dirs: list[Path]) -> list[JobIngestResult]:
    """Ingest multiple job directories. Each job is isolated in its own
    SAVEPOINT: a malformed job is rolled back and reported, without
    disturbing data already committed for other jobs."""
    results: list[JobIngestResult] = []
    for job_dir in job_dirs:
        job_dir = Path(job_dir)
        conn.execute("SAVEPOINT job_ingest")
        try:
            result = ingest_job(conn, job_dir)
            conn.execute("RELEASE SAVEPOINT job_ingest")
            conn.commit()
            results.append(result)
        except IngestError as exc:
            conn.execute("ROLLBACK TO SAVEPOINT job_ingest")
            conn.execute("RELEASE SAVEPOINT job_ingest")
            results.append(
                JobIngestResult(job_dir=str(job_dir), run_key=job_dir.name, ok=False, error=str(exc))
            )
    return results
