"""Public and private task/system report generation.

Private reports may include individual attempts, failed verifier-check
names, job/trial identifiers, raw artifact references, and exception
details. Public reports are redacted to aggregate-only figures and must
never contain local filesystem paths, individual hidden-check names,
oracle/verifier content, trajectory contents, API information, private task
contents, or secrets/environment-variable values.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from coldctl.results.aggregate import compute_aggregate, compute_aggregate_for_trial_keys
from coldctl.results.constants import DIMENSIONS

VISIBILITIES = ("public", "private")
FORMATS = ("json", "markdown")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trial_ids_and_rows(conn: sqlite3.Connection, *, task: str, system: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT trials.*, runs.run_key AS run_key
        FROM trials
        JOIN task_versions ON task_versions.id = trials.task_version_id
        JOIN tasks ON tasks.id = task_versions.task_id
        JOIN systems ON systems.id = trials.system_id
        JOIN runs ON runs.id = trials.run_id
        WHERE tasks.name = ? AND systems.system_key = ?
        ORDER BY trials.started_at
        """,
        (task, system),
    ).fetchall()


def _trial_rows_for_keys(conn: sqlite3.Connection, trial_keys: list[str]) -> list[sqlite3.Row]:
    """Same shape as :func:`_trial_ids_and_rows`, but scoped to an explicit
    set of trial_keys rather than task/system identity. Assumes the keys
    have already been validated (e.g. via compute_aggregate_for_trial_keys)."""
    placeholders = ", ".join("?" for _ in trial_keys)
    return conn.execute(
        f"""
        SELECT trials.*, runs.run_key AS run_key
        FROM trials
        JOIN runs ON runs.id = trials.run_id
        WHERE trials.trial_key IN ({placeholders})
        ORDER BY trials.started_at
        """,
        list(trial_keys),
    ).fetchall()


def _dimension_scores_for(conn: sqlite3.Connection, trial_id: int) -> dict[str, float]:
    rows = conn.execute(
        "SELECT dimension, value FROM dimension_scores WHERE trial_id = ?", (trial_id,)
    ).fetchall()
    return {row["dimension"]: row["value"] for row in rows}


def _failed_checks_for(conn: sqlite3.Connection, trial_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT check_name FROM verifier_checks WHERE trial_id = ? AND passed = 0 ORDER BY check_name",
        (trial_id,),
    ).fetchall()
    return [row["check_name"] for row in rows]


def _artifacts_for(conn: sqlite3.Connection, trial_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT kind, source_path, sha256, size_bytes FROM artifact_references "
        "WHERE trial_id = ? ORDER BY kind",
        (trial_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _system_configuration(conn: sqlite3.Connection, *, system: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT models.name AS model_name, models.provider AS model_provider,
               agents.name AS agent_name, agents.version AS agent_version,
               systems.agent_kwargs_json AS agent_kwargs_json
        FROM systems
        JOIN models ON models.id = systems.model_id
        JOIN agents ON agents.id = systems.agent_id
        WHERE systems.system_key = ?
        """,
        (system,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "model_name": row["model_name"],
        "model_provider": row["model_provider"],
        "agent_name": row["agent_name"],
        "agent_version": row["agent_version"],
        "agent_kwargs": json.loads(row["agent_kwargs_json"] or "{}"),
    }


def _evaluation_date_range(rows: list[sqlite3.Row]) -> dict[str, str | None]:
    started = sorted(r["started_at"] for r in rows if r["started_at"])
    if not started:
        return {"from": None, "to": None}
    return {"from": started[0][:10], "to": started[-1][:10]}


def build_private_report(
    conn: sqlite3.Connection,
    *,
    task: str,
    system: str,
    trial_keys: list[str] | None = None,
    orchestration_run_id: str | None = None,
) -> dict[str, Any]:
    """Build a private report. When ``trial_keys`` is given, the report is
    scoped to exactly those trials (see
    ``compute_aggregate_for_trial_keys``); otherwise it falls back to
    Phase 1's all-history aggregation for (task, system) -- the same
    behavior as before trial-key scoping existed, preserved for the
    existing ``coldctl reports task`` command."""
    if trial_keys is not None:
        aggregate = compute_aggregate_for_trial_keys(conn, trial_keys, task=task, system=system)
        rows = _trial_rows_for_keys(conn, trial_keys)
    else:
        aggregate = compute_aggregate(conn, task=task, system=system)
        rows = _trial_ids_and_rows(conn, task=task, system=system)

    attempts = []
    for row in rows:
        trial_id = row["id"]
        exception = None
        if row["is_infra_exception"]:
            exception = {
                "type": row["exception_type"],
                "message": row["exception_message"],
                "occurred_at": row["exception_occurred_at"],
            }
        attempts.append(
            {
                "trial_key": row["trial_key"],
                "trial_name": row["trial_name"],
                "run_key": row["run_key"],
                "trial_uuid": row["trial_uuid"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "runtime_sec": row["runtime_sec"],
                "runtime_basis": row["runtime_basis"],
                "strict_pass": bool(row["strict_pass"]) if row["strict_pass"] is not None else None,
                "coldstart_pass": row["coldstart_pass"],
                "dimension_scores": _dimension_scores_for(conn, trial_id),
                "failed_checks": _failed_checks_for(conn, trial_id),
                "cost_usd": row["cost_usd"],
                "tokens": {
                    "input": row["input_tokens"],
                    "output": row["output_tokens"],
                    "cached": row["cached_tokens"],
                },
                "is_infra_exception": bool(row["is_infra_exception"]),
                "exception": exception,
                "artifacts": _artifacts_for(conn, trial_id),
            }
        )

    report = {
        "schema": "coldstart.private_report.v1",
        "visibility": "private",
        "task": task,
        "system": system,
        "system_configuration": _system_configuration(conn, system=system),
        "generated_at": _now_iso(),
        "evaluation_date_range": _evaluation_date_range(rows),
        "aggregate": aggregate.to_dict(),
        "attempts": attempts,
    }
    if orchestration_run_id is not None:
        report["orchestration_run_id"] = orchestration_run_id
    return report


def build_public_report(
    conn: sqlite3.Connection,
    *,
    task: str,
    system: str,
    trial_keys: list[str] | None = None,
    orchestration_run_id: str | None = None,
) -> dict[str, Any]:
    """See ``build_private_report`` for the ``trial_keys``/
    ``orchestration_run_id`` scoping behavior."""
    if trial_keys is not None:
        aggregate = compute_aggregate_for_trial_keys(conn, trial_keys, task=task, system=system)
        rows = _trial_rows_for_keys(conn, trial_keys)
    else:
        aggregate = compute_aggregate(conn, task=task, system=system)
        rows = _trial_ids_and_rows(conn, task=task, system=system)
    aggregate_dict = aggregate.to_dict()

    report = {
        "schema": "coldstart.public_report.v1",
        "visibility": "public",
        "task": task,
        "system": system,
        "system_configuration": _system_configuration(conn, system=system),
        "benchmark_versions": aggregate_dict["benchmark_versions"],
        "task_versions": aggregate_dict["task_versions"],
        "generated_at": _now_iso(),
        "evaluation_date_range": _evaluation_date_range(rows),
        "attempts": aggregate_dict["attempts"],
        "scored_attempts": aggregate_dict["scored_attempts"],
        "strict_pass_rate": aggregate_dict["strict_pass_rate"],
        "dimension_averages": aggregate_dict["dimension_averages"],
        "cost": aggregate_dict["cost"],
        "runtime": aggregate_dict["runtime"],
        "tokens": aggregate_dict["tokens"],
        "failure_totals": {
            "failures": aggregate_dict["failures"],
            "exceptions": aggregate_dict["exception_count"],
        },
    }
    if orchestration_run_id is not None:
        report["orchestration_run_id"] = orchestration_run_id
    return report


def build_report(
    conn: sqlite3.Connection,
    *,
    task: str,
    system: str,
    visibility: str,
    trial_keys: list[str] | None = None,
    orchestration_run_id: str | None = None,
) -> dict[str, Any]:
    if visibility not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {VISIBILITIES}, got {visibility!r}")
    if visibility == "private":
        return build_private_report(
            conn, task=task, system=system, trial_keys=trial_keys, orchestration_run_id=orchestration_run_id
        )
    return build_public_report(
        conn, task=task, system=system, trial_keys=trial_keys, orchestration_run_id=orchestration_run_id
    )


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    visibility = report["visibility"]
    lines.append(f"# ColdStart task report — {report['task']} / {report['system']}")
    lines.append("")
    lines.append(f"- Visibility: **{visibility}**")
    if report.get("orchestration_run_id"):
        lines.append(f"- ColdStart orchestration run ID: {report['orchestration_run_id']}")
    lines.append(f"- Generated at: {report['generated_at']}")
    date_range = report.get("evaluation_date_range", {})
    lines.append(f"- Evaluation date range: {date_range.get('from')} to {date_range.get('to')}")
    if report.get("benchmark_versions"):
        lines.append(f"- Benchmark version(s): {', '.join(report['benchmark_versions'])}")
    if report.get("task_versions"):
        lines.append(f"- Task digest(s): {', '.join(report['task_versions'])}")
    config = report.get("system_configuration") or {}
    if config:
        lines.append(
            f"- Model: {config.get('model_name')} (provider: {config.get('model_provider')})"
        )
        lines.append(f"- Agent: {config.get('agent_name')} {config.get('agent_version') or ''}".rstrip())
        if config.get("agent_kwargs"):
            lines.append(f"- Agent settings: `{json.dumps(config['agent_kwargs'], sort_keys=True)}`")
    lines.append("")

    if visibility == "private":
        aggregate = report["aggregate"]
    else:
        aggregate = report

    lines.append("## Aggregate results")
    lines.append("")
    lines.append(f"- Attempts: {aggregate.get('attempts', report.get('attempts'))}")
    lines.append(f"- Scored attempts: {aggregate.get('scored_attempts')}")
    if visibility == "private":
        lines.append(f"- Passes: {aggregate.get('passes')}")
        lines.append(f"- Failures: {aggregate.get('failures')}")
    else:
        lines.append(f"- Failures: {aggregate['failure_totals']['failures']}")
    lines.append(f"- Strict pass rate: {_fmt(aggregate.get('strict_pass_rate'))}")
    lines.append(
        f"- Exceptions: {aggregate.get('exception_count', aggregate.get('failure_totals', {}).get('exceptions'))}"
    )
    lines.append("")
    lines.append("### Dimension averages (diagnostic only, not part of strict pass)")
    lines.append("")
    lines.append("| Dimension | Average |")
    lines.append("|---|---|")
    dim_scores = aggregate.get("dimension_averages", {})
    for dimension in DIMENSIONS:
        if dimension in dim_scores:
            lines.append(f"| {dimension} | {_fmt(dim_scores[dimension])} |")
    lines.append("")
    cost = aggregate.get("cost", {})
    runtime = aggregate.get("runtime", {})
    tokens = aggregate.get("tokens", {})
    lines.append("### Cost, runtime, tokens")
    lines.append("")
    lines.append(f"- Total cost (USD): {_fmt(cost.get('total_usd'))}")
    lines.append(f"- Average cost (USD): {_fmt(cost.get('average_usd'))}")
    lines.append(f"- Median runtime (sec): {_fmt(runtime.get('median_sec'), 1)}")
    lines.append(f"- Total input tokens: {_fmt(tokens.get('total_input'), 0)}")
    lines.append(f"- Total output tokens: {_fmt(tokens.get('total_output'), 0)}")
    lines.append(f"- Total cached tokens: {_fmt(tokens.get('total_cached'), 0)}")
    lines.append("")

    if visibility == "private":
        failed_checks = aggregate.get("failed_check_counts", {})
        if failed_checks:
            lines.append("### Failed verifier checks (private)")
            lines.append("")
            lines.append("| Check | Failures |")
            lines.append("|---|---|")
            for check_name, count in sorted(failed_checks.items()):
                lines.append(f"| {check_name} | {count} |")
            lines.append("")

        lines.append("### Individual attempts (private)")
        lines.append("")
        lines.append("| Trial | Strict pass | Failed checks | Cost (USD) | Runtime (s) | Exception |")
        lines.append("|---|---|---|---|---|---|")
        for attempt in report["attempts"]:
            failed = ", ".join(attempt["failed_checks"]) or "-"
            exc = attempt["exception"]["type"] if attempt["exception"] else "-"
            lines.append(
                f"| {attempt['trial_key']} | {attempt['strict_pass']} | {failed} | "
                f"{_fmt(attempt['cost_usd'])} | {_fmt(attempt['runtime_sec'], 1)} | {exc} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"
