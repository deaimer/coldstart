"""Frozen run manifest + mutable run state, with atomic on-disk writes.

Layout per run, under ``.coldstart/runs/<run-id>/``::

    manifest.json   -- written once, never overwritten
    state.json      -- rewritten atomically on every state change
    events.jsonl    -- append-only event log
    logs/           -- per-trial harbor stdout/stderr (redacted)
    artifacts/      -- reserved for future use
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1

RUN_STATUSES = ("planned", "running", "completed", "failed", "paused", "cancelled")
TRIAL_STATUSES = (
    "pending",
    "running",
    "passed",
    "failed",
    "infra_invalid_retry_scheduled",
    "infra_invalid_exhausted",
    "auth_error_paused",
    "unknown_paused",
)


class ManifestExistsError(Exception):
    """Raised when attempting to create a run manifest that already exists."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: write to a sibling temp file, fsync, then
    os.replace onto the destination -- readers never observe a partial write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def append_event(events_path: Path, event: dict[str, Any]) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"schema_version": EVENT_SCHEMA_VERSION, **event}, sort_keys=True)
    with events_path.open("a") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@dataclass
class TrialState:
    trial_id: str
    status: str = "pending"
    attempts: int = 0
    harbor_job_dirs: list[str] = field(default_factory=list)
    outcome_reason: str | None = None
    evidence: str | None = None
    cost_usd: float | None = None
    ingested: bool = False
    last_updated: str = field(default_factory=_now_iso)


@dataclass
class ReportStatus:
    generated: bool = False
    path: str | None = None


@dataclass
class RunState:
    schema_version: int
    run_id: str
    status: str
    trials: dict[str, TrialState]
    invalid_infrastructure_attempts: int = 0
    actual_cost_usd: float = 0.0
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    last_event: dict[str, Any] | None = None
    private_report: ReportStatus = field(default_factory=ReportStatus)
    public_report: ReportStatus = field(default_factory=ReportStatus)

    @property
    def completed_trial_ids(self) -> list[str]:
        return [tid for tid, t in self.trials.items() if t.status in ("passed", "failed", "infra_invalid_exhausted")]

    @property
    def pending_trial_ids(self) -> list[str]:
        return [
            tid
            for tid, t in self.trials.items()
            if t.status in ("pending", "infra_invalid_retry_scheduled")
        ]

    @property
    def retry_counts(self) -> dict[str, int]:
        return {tid: t.attempts for tid, t in self.trials.items() if t.attempts > 0}

    @property
    def harbor_job_dirs_by_trial(self) -> dict[str, list[str]]:
        return {tid: list(t.harbor_job_dirs) for tid, t in self.trials.items() if t.harbor_job_dirs}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "trials": {tid: asdict(t) for tid, t in self.trials.items()},
            "completed_trial_ids": self.completed_trial_ids,
            "pending_trial_ids": self.pending_trial_ids,
            "invalid_infrastructure_attempts": self.invalid_infrastructure_attempts,
            "retry_counts": self.retry_counts,
            "actual_cost_usd": self.actual_cost_usd,
            "harbor_job_dirs": self.harbor_job_dirs_by_trial,
            "ingestion_status": {tid: t.ingested for tid, t in self.trials.items()},
            "report_status": {
                "private": asdict(self.private_report),
                "public": asdict(self.public_report),
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_event": self.last_event,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        trials = {
            tid: TrialState(**trial_data) for tid, trial_data in data.get("trials", {}).items()
        }
        report_status = data.get("report_status") or {}
        return cls(
            schema_version=data.get("schema_version", STATE_SCHEMA_VERSION),
            run_id=data["run_id"],
            status=data["status"],
            trials=trials,
            invalid_infrastructure_attempts=data.get("invalid_infrastructure_attempts", 0),
            actual_cost_usd=data.get("actual_cost_usd", 0.0),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
            last_event=data.get("last_event"),
            private_report=ReportStatus(**(report_status.get("private") or {})),
            public_report=ReportStatus(**(report_status.get("public") or {})),
        )


def new_state(run_id: str, trial_ids: list[str]) -> RunState:
    return RunState(
        schema_version=STATE_SCHEMA_VERSION,
        run_id=run_id,
        status="planned",
        trials={tid: TrialState(trial_id=tid) for tid in trial_ids},
    )


def run_dir_for(coldstart_dir: Path, run_id: str) -> Path:
    return Path(coldstart_dir) / "runs" / run_id


def manifest_path(run_dir: Path) -> Path:
    return run_dir / "manifest.json"


def state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def events_path(run_dir: Path) -> Path:
    return run_dir / "events.jsonl"


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    path = manifest_path(run_dir)
    if path.exists():
        raise ManifestExistsError(f"run manifest already exists and is immutable: {path}")
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, manifest)


def read_manifest(run_dir: Path) -> dict[str, Any]:
    path = manifest_path(run_dir)
    if not path.is_file():
        raise FileNotFoundError(f"no manifest found for run at {run_dir}")
    return json.loads(path.read_text())


def write_state(run_dir: Path, state: RunState) -> None:
    state.updated_at = _now_iso()
    atomic_write_json(state_path(run_dir), state.to_dict())


def read_state(run_dir: Path) -> RunState:
    path = state_path(run_dir)
    if not path.is_file():
        raise FileNotFoundError(f"no state found for run at {run_dir}")
    return RunState.from_dict(json.loads(path.read_text()))
