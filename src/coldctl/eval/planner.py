"""Deterministic trial expansion and cost estimation for `eval plan`/`eval run`.

Nothing here calls Harbor. Cost estimation reads Phase 1's SQLite results
store (if present) but never touches the network or a paid API.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coldctl.eval.config import EvaluationConfig, SystemConfig, config_to_dict
from coldctl.eval.git_info import GitInfo, get_git_info
from coldctl.eval.hashing import canonical_json_hash, hash_task_directory
from coldctl.results.aggregate import compute_aggregate


class DirtyWorktreeError(Exception):
    """Raised when an official evaluation is planned from a dirty/unverifiable worktree."""


@dataclass
class TrialSpec:
    trial_id: str
    task_path: str
    task_name: str
    system_key: str
    provider: str
    model: str
    agent: str
    environment: str
    agent_kwargs: dict[str, Any]
    api_key_env: str
    attempt: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "task_path": self.task_path,
            "task_name": self.task_name,
            "system_key": self.system_key,
            "provider": self.provider,
            "model": self.model,
            "agent": self.agent,
            "environment": self.environment,
            "agent_kwargs": dict(self.agent_kwargs),
            "api_key_env": self.api_key_env,
            "attempt": self.attempt,
        }


@dataclass
class CostBreakdownEntry:
    task_name: str
    system_key: str
    trials: int
    estimate_usd: float | None
    source: str  # "historical" | "configured_estimate" | "unavailable"


@dataclass
class CostEstimate:
    total_usd: float | None
    source: str  # "historical" | "configured_estimate" | "mixed" | "partial" | "unavailable"
    breakdown: list[CostBreakdownEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_usd": self.total_usd,
            "source": self.source,
            "breakdown": [
                {
                    "task_name": e.task_name,
                    "system_key": e.system_key,
                    "trials": e.trials,
                    "estimate_usd": e.estimate_usd,
                    "source": e.source,
                }
                for e in self.breakdown
            ],
        }


@dataclass
class Plan:
    config_hash: str
    benchmark_version: str
    git_commit: str | None
    git_dirty: bool
    git_available: bool
    unverified: bool
    task_hashes: dict[str, str]
    trials: list[TrialSpec]
    cost_estimate: CostEstimate

    @property
    def total_planned_trials(self) -> int:
        return len(self.trials)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_hash": self.config_hash,
            "benchmark_version": self.benchmark_version,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "git_available": self.git_available,
            "unverified": self.unverified,
            "task_hashes": dict(self.task_hashes),
            "total_planned_trials": self.total_planned_trials,
            "trials": [t.to_dict() for t in self.trials],
            "cost_estimate": self.cost_estimate.to_dict(),
        }


def compute_config_hash(config: EvaluationConfig) -> str:
    return canonical_json_hash(config_to_dict(config))


def _task_name_from_path(task_path: str) -> str:
    return Path(task_path).name


def _short_hash(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return digest[:10]


def expand_trials(config: EvaluationConfig, config_hash: str) -> list[TrialSpec]:
    """Deterministic: identical config content -> identical trial_ids, in a
    stable order (task, then system, then attempt)."""
    trials: list[TrialSpec] = []
    for task_path in config.tasks:
        task_name = _task_name_from_path(task_path)
        for system in config.systems:
            system_key = system.system_key
            for attempt in range(1, system.trials_per_task + 1):
                digest = _short_hash(config_hash, task_path, system_key, str(attempt))
                trial_id = f"{task_name}__{system_key}__{attempt:02d}_{digest}"
                trials.append(
                    TrialSpec(
                        trial_id=trial_id,
                        task_path=task_path,
                        task_name=task_name,
                        system_key=system_key,
                        provider=system.provider,
                        model=system.model,
                        agent=system.agent,
                        environment=system.environment,
                        agent_kwargs=dict(system.agent_kwargs),
                        api_key_env=system.api_key_env,
                        attempt=attempt,
                    )
                )
    return trials


def hash_tasks(repo_root: Path, task_paths: list[str]) -> dict[str, str]:
    return {task_path: hash_task_directory(repo_root / task_path) for task_path in task_paths}


def _historical_estimate(
    phase1_db_path: Path, *, task_name: str, system_key: str, trials: int
) -> float | None:
    if not Path(phase1_db_path).is_file():
        return None
    from coldctl.results import db as db_module

    conn = db_module.connect(phase1_db_path)
    try:
        aggregate = compute_aggregate(conn, task=task_name, system=system_key)
    except ValueError:
        return None
    finally:
        conn.close()
    if aggregate.average_cost_usd is None:
        return None
    return aggregate.average_cost_usd * trials


def estimate_cost(
    config: EvaluationConfig, *, phase1_db_path: Path
) -> CostEstimate:
    breakdown: list[CostBreakdownEntry] = []
    for task_path in config.tasks:
        task_name = _task_name_from_path(task_path)
        for system in config.systems:
            system_key = system.system_key
            historical = _historical_estimate(
                phase1_db_path, task_name=task_name, system_key=system_key, trials=system.trials_per_task
            )
            if historical is not None:
                breakdown.append(
                    CostBreakdownEntry(task_name, system_key, system.trials_per_task, historical, "historical")
                )
            elif system.estimated_cost_per_trial_usd is not None:
                breakdown.append(
                    CostBreakdownEntry(
                        task_name,
                        system_key,
                        system.trials_per_task,
                        system.estimated_cost_per_trial_usd * system.trials_per_task,
                        "configured_estimate",
                    )
                )
            else:
                breakdown.append(
                    CostBreakdownEntry(task_name, system_key, system.trials_per_task, None, "unavailable")
                )

    sources = {entry.source for entry in breakdown}
    if not breakdown or sources == {"unavailable"}:
        return CostEstimate(total_usd=None, source="unavailable", breakdown=breakdown)

    available = [e for e in breakdown if e.estimate_usd is not None]
    total = sum(e.estimate_usd for e in available) if available else None
    if sources == {"historical"}:
        source = "historical"
    elif sources == {"configured_estimate"}:
        source = "configured_estimate"
    elif "unavailable" in sources:
        source = "partial"
    else:
        source = "mixed"
    return CostEstimate(total_usd=total, source=source, breakdown=breakdown)


def build_plan(
    config: EvaluationConfig,
    *,
    repo_root: Path,
    phase1_db_path: Path,
    allow_dirty: bool = False,
) -> Plan:
    config_hash = compute_config_hash(config)
    git_info: GitInfo = get_git_info(repo_root)
    trials = expand_trials(config, config_hash)
    task_hashes = hash_tasks(repo_root, config.tasks)
    cost_estimate = estimate_cost(config, phase1_db_path=phase1_db_path)

    unverified = (not git_info.available) or git_info.dirty
    if config.is_official and unverified and not allow_dirty:
        raise DirtyWorktreeError(
            "official evaluations require a clean git worktree with a resolvable commit; "
            "re-run with --allow-dirty for a development/unverified plan, or commit first"
        )

    return Plan(
        config_hash=config_hash,
        benchmark_version=config.benchmark_version,
        git_commit=git_info.commit,
        git_dirty=git_info.dirty,
        git_available=git_info.available,
        unverified=unverified,
        task_hashes=task_hashes,
        trials=trials,
        cost_estimate=cost_estimate,
    )
