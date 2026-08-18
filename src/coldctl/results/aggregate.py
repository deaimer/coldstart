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


def compute_aggregate(conn: sqlite3.Connection, *, task: str, system: str) -> TaskSystemAggregate:
    trial_ids = _trial_ids_for(conn, task=task, system=system)
    if not trial_ids:
        raise ValueError(f"No ingested trials found for task={task!r} system={system!r}")

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
