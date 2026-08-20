"""Aggregation of normalized trial data into task/system summaries.

Strict pass is computed only from the ``coldstart_pass`` reward key
(``trials.strict_pass`` / ``trials.coldstart_pass``). The five diagnostic
dimensions in :data:`coldctl.results.constants.DIMENSIONS` are reported
separately and never blended into the strict pass rate.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Any

from coldctl.results.constants import DIMENSIONS


@dataclass
class TaskSystemAggregate:
    task: str
    system: str
    task_versions: list[str]
    benchmark_versions: list[str]
    attempts: int
    scored_attempts: int
    passes: int
    failures: int
    strict_pass_rate: float | None
    dimension_averages: dict[str, float]
    exception_count: int
    total_cost_usd: float | None
    average_cost_usd: float | None
    cost_sample_count: int
    median_runtime_sec: float | None
    runtime_sample_count: int
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_cached_tokens: int | None
    failed_check_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "system": self.system,
            "task_versions": self.task_versions,
            "benchmark_versions": self.benchmark_versions,
            "attempts": self.attempts,
            "scored_attempts": self.scored_attempts,
            "passes": self.passes,
            "failures": self.failures,
            "strict_pass_rate": self.strict_pass_rate,
            "dimension_averages": self.dimension_averages,
            "exception_count": self.exception_count,
            "cost": {
                "total_usd": self.total_cost_usd,
                "average_usd": self.average_cost_usd,
                "sample_count": self.cost_sample_count,
            },
            "runtime": {
                "median_sec": self.median_runtime_sec,
                "sample_count": self.runtime_sample_count,
            },
            "tokens": {
                "total_input": self.total_input_tokens,
                "total_output": self.total_output_tokens,
                "total_cached": self.total_cached_tokens,
            },
            "failed_check_counts": self.failed_check_counts,
        }


class EmptyTrialSelectionError(ValueError):
    """Raised when an explicit trial-key/trial-id selection is empty.
    Aggregation must never silently fall back to all-history in this case
    (see :func:`compute_aggregate_for_trial_keys`)."""


class UnknownTrialKeysError(ValueError):
    """Raised when one or more explicitly-selected trial keys do not exist
    in the results store."""

    def __init__(self, missing_keys: list[str]) -> None:
        self.missing_keys = list(missing_keys)
        super().__init__(f"unknown trial key(s): {self.missing_keys}")


def _trial_ids_for(conn: sqlite3.Connection, *, task: str, system: str) -> list[int]:
    rows = conn.execute(
        """
        SELECT trials.id AS id
        FROM trials
        JOIN task_versions ON task_versions.id = trials.task_version_id
        JOIN tasks ON tasks.id = task_versions.task_id
        JOIN systems ON systems.id = trials.system_id
        WHERE tasks.name = ? AND systems.system_key = ?
        """,
        (task, system),
    ).fetchall()
    return [row["id"] for row in rows]


def _trial_ids_for_keys(conn: sqlite3.Connection, trial_keys: list[str]) -> list[int]:
    """Resolve explicit Phase 1 trial_keys to row ids, in the given order.
    Raises UnknownTrialKeysError if any key is not present in the store."""
    placeholders = ", ".join("?" for _ in trial_keys)
    rows = conn.execute(
        f"SELECT id, trial_key FROM trials WHERE trial_key IN ({placeholders})", trial_keys
    ).fetchall()
    found = {row["trial_key"]: row["id"] for row in rows}
    missing = [key for key in trial_keys if key not in found]
    if missing:
        raise UnknownTrialKeysError(missing)
    return [found[key] for key in trial_keys]


def _derive_task_system_labels(
    conn: sqlite3.Connection, trial_ids: list[int]
) -> tuple[str, str]:
    placeholders = ", ".join("?" for _ in trial_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT tasks.name AS task_name, systems.system_key AS system_key
        FROM trials
        JOIN task_versions ON task_versions.id = trials.task_version_id
        JOIN tasks ON tasks.id = task_versions.task_id
        JOIN systems ON systems.id = trials.system_id
        WHERE trials.id IN ({placeholders})
        """,
        trial_ids,
    ).fetchall()
    task_names = sorted({row["task_name"] for row in rows})
    system_keys = sorted({row["system_key"] for row in rows})
    task = task_names[0] if len(task_names) == 1 else "+".join(task_names)
    system = system_keys[0] if len(system_keys) == 1 else "+".join(system_keys)
    return task, system


def compute_aggregate(conn: sqlite3.Connection, *, task: str, system: str) -> TaskSystemAggregate:
    """All-history aggregation for a (task, system) pair. This is an
    explicit call path (see module docstring): it is never invoked
    automatically by the Phase 2 evaluation runner, which always scopes its
    own reports to an explicit trial-key selection via
    :func:`compute_aggregate_for_trial_keys`."""
    trial_ids = _trial_ids_for(conn, task=task, system=system)
    if not trial_ids:
        raise ValueError(f"No ingested trials found for task={task!r} system={system!r}")
    return _aggregate_from_trial_ids(conn, trial_ids, task=task, system=system)


def compute_aggregate_for_trial_keys(
    conn: sqlite3.Connection,
    trial_keys: list[str],
    *,
    task: str | None = None,
    system: str | None = None,
) -> TaskSystemAggregate:
    """Aggregation scoped to an explicit, caller-supplied set of Phase 1
    trial keys -- never inferred from task/system identity alone. Used by
    the Phase 2 evaluation runner so a run's automatic report only ever
    reflects the trials that run itself produced, regardless of how much
    other history exists for the same (task, system) pair.

    Raises :class:`EmptyTrialSelectionError` for an empty selection (never
    silently falls back to all-history) and :class:`UnknownTrialKeysError`
    if any key does not exist in the store.
    """
    if not trial_keys:
        raise EmptyTrialSelectionError(
            "no trial keys provided; refusing to aggregate over all history"
        )
    trial_ids = _trial_ids_for_keys(conn, list(trial_keys))
    if task is None or system is None:
        derived_task, derived_system = _derive_task_system_labels(conn, trial_ids)
        task = task if task is not None else derived_task
        system = system if system is not None else derived_system
    return _aggregate_from_trial_ids(conn, trial_ids, task=task, system=system)


def _aggregate_from_trial_ids(
    conn: sqlite3.Connection, trial_ids: list[int], *, task: str, system: str
) -> TaskSystemAggregate:
    placeholders = ", ".join("?" for _ in trial_ids)

    task_versions = [
        row["digest"]
        for row in conn.execute(
            f"""
            SELECT DISTINCT task_versions.digest AS digest
            FROM trials JOIN task_versions ON task_versions.id = trials.task_version_id
            WHERE trials.id IN ({placeholders})
            """,
            trial_ids,
        ).fetchall()
    ]
    benchmark_versions = [
        row["version"]
        for row in conn.execute(
            f"""
            SELECT DISTINCT benchmark_versions.version AS version
            FROM trials
            JOIN runs ON runs.id = trials.run_id
            JOIN benchmark_versions ON benchmark_versions.id = runs.benchmark_version_id
            WHERE trials.id IN ({placeholders})
            """,
            trial_ids,
        ).fetchall()
    ]

    attempts = len(trial_ids)

    scored_rows = conn.execute(
        f"SELECT strict_pass FROM trials WHERE id IN ({placeholders}) AND coldstart_pass IS NOT NULL",
        trial_ids,
    ).fetchall()
    scored_attempts = len(scored_rows)
    passes = sum(1 for row in scored_rows if row["strict_pass"] == 1)
    failures = scored_attempts - passes
    strict_pass_rate = (passes / scored_attempts) if scored_attempts else None

    dimension_averages: dict[str, float] = {}
    for dimension in DIMENSIONS:
        row = conn.execute(
            f"""
            SELECT AVG(value) AS avg_value
            FROM dimension_scores
            WHERE dimension = ? AND trial_id IN ({placeholders})
            """,
            [dimension, *trial_ids],
        ).fetchone()
        if row["avg_value"] is not None:
            dimension_averages[dimension] = row["avg_value"]

    exception_count = conn.execute(
        f"SELECT COUNT(*) AS n FROM trials WHERE id IN ({placeholders}) AND is_infra_exception = 1",
        trial_ids,
    ).fetchone()["n"]

    cost_rows = [
        row["cost_usd"]
        for row in conn.execute(
            f"SELECT cost_usd FROM trials WHERE id IN ({placeholders}) AND cost_usd IS NOT NULL",
            trial_ids,
        ).fetchall()
    ]
    total_cost = sum(cost_rows) if cost_rows else None
    average_cost = (total_cost / len(cost_rows)) if cost_rows else None

    runtime_rows = [
        row["runtime_sec"]
        for row in conn.execute(
            f"SELECT runtime_sec FROM trials WHERE id IN ({placeholders}) AND runtime_sec IS NOT NULL",
            trial_ids,
        ).fetchall()
    ]
    median_runtime = statistics.median(runtime_rows) if runtime_rows else None

    def _sum_tokens(column: str) -> int | None:
        row = conn.execute(
            f"SELECT SUM({column}) AS total FROM trials WHERE id IN ({placeholders})",
            trial_ids,
        ).fetchone()
        return row["total"]

    total_input_tokens = _sum_tokens("input_tokens")
    total_output_tokens = _sum_tokens("output_tokens")
    total_cached_tokens = _sum_tokens("cached_tokens")

    failed_check_counts: dict[str, int] = {}
    for row in conn.execute(
        f"""
        SELECT check_name, COUNT(*) AS n
        FROM verifier_checks
        WHERE trial_id IN ({placeholders}) AND passed = 0
        GROUP BY check_name
        ORDER BY check_name
        """,
        trial_ids,
    ).fetchall():
        failed_check_counts[row["check_name"]] = row["n"]

    return TaskSystemAggregate(
        task=task,
        system=system,
        task_versions=sorted(task_versions),
        benchmark_versions=sorted(v for v in benchmark_versions if v is not None),
        attempts=attempts,
        scored_attempts=scored_attempts,
        passes=passes,
        failures=failures,
        strict_pass_rate=strict_pass_rate,
        dimension_averages=dimension_averages,
        exception_count=exception_count,
        total_cost_usd=total_cost,
        average_cost_usd=average_cost,
        cost_sample_count=len(cost_rows),
        median_runtime_sec=median_runtime,
        runtime_sample_count=len(runtime_rows),
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cached_tokens=total_cached_tokens,
        failed_check_counts=failed_check_counts,
    )
