"""Typer commands for the ColdStart evaluation-results system."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from coldctl.results import db as db_module
from coldctl.results.aggregate import compute_aggregate
from coldctl.results.ingest import ingest_jobs
from coldctl.results.reports import build_report, render_json, render_markdown

results_app = typer.Typer(help="Ingest and inspect normalized Harbor evaluation results.", no_args_is_help=True)
reports_app = typer.Typer(help="Generate public/private task reports.", no_args_is_help=True)

console = Console()
error_console = Console(stderr=True)

DB_OPTION = typer.Option(
    db_module.DEFAULT_DB_PATH, "--db", help="Path to the ColdStart results SQLite database."
)


@results_app.command("ingest")
def ingest(
    job_dirs: list[Path] = typer.Argument(..., help="One or more completed Harbor job directories."),
    db: Path = DB_OPTION,
) -> None:
    """Ingest one or more completed Harbor job directories (idempotent)."""
    conn = db_module.connect(db)
    try:
        results = ingest_jobs(conn, job_dirs)
    finally:
        conn.close()

    failed = False
    for result in results:
        if result.ok:
            n_created = sum(1 for t in result.trials if t.created)
            n_updated = len(result.trials) - n_created
            console.print(
                f"[green]OK[/green] {result.job_dir}: run '{result.run_key}' "
                f"({n_created} trial(s) added, {n_updated} updated)"
            )
        else:
            failed = True
            error_console.print(f"[red]ERROR[/red] {result.job_dir}: {result.error}")

    if failed:
        raise typer.Exit(1)


@results_app.command("list-runs")
def list_runs(
    db: Path = DB_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """List ingested evaluation runs (Harbor jobs)."""
    conn = db_module.connect(db)
    try:
        rows = conn.execute(
            """
            SELECT runs.run_key AS run_key, runs.job_uuid AS job_uuid,
                   runs.started_at AS started_at, runs.n_total_trials AS n_total_trials,
                   runs.n_completed_trials AS n_completed_trials,
                   runs.n_errored_trials AS n_errored_trials,
                   benchmark_versions.version AS benchmark_version
            FROM runs
            LEFT JOIN benchmark_versions ON benchmark_versions.id = runs.benchmark_version_id
            ORDER BY runs.started_at
            """
        ).fetchall()
    finally:
        conn.close()

    payload = [dict(row) for row in rows]
    if as_json:
        console.print_json(json.dumps(payload))
        return

    columns = ["run_key", "job_uuid", "started_at", "n_total_trials", "n_completed_trials", "n_errored_trials", "benchmark_version"]
    table = Table(title="Evaluation runs")
    for column in columns:
        table.add_column(column)
    for row in payload:
        table.add_row(*(str(row[c]) if row[c] is not None else "" for c in columns))
    console.print(table)


@results_app.command("list-trials")
def list_trials(
    db: Path = DB_OPTION,
    task: str | None = typer.Option(None, "--task", help="Filter by task name."),
    system: str | None = typer.Option(None, "--system", help="Filter by system key (model__agent)."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """List ingested trials, optionally filtered by task and/or system."""
    conn = db_module.connect(db)
    try:
        clauses = []
        params: list[str] = []
        if task:
            clauses.append("tasks.name = ?")
            params.append(task)
        if system:
            clauses.append("systems.system_key = ?")
            params.append(system)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT trials.trial_key AS trial_key, tasks.name AS task,
                   systems.system_key AS system, trials.strict_pass AS strict_pass,
                   trials.coldstart_pass AS coldstart_pass,
                   trials.runtime_sec AS runtime_sec, trials.cost_usd AS cost_usd,
                   trials.is_infra_exception AS is_infra_exception
            FROM trials
            JOIN task_versions ON task_versions.id = trials.task_version_id
            JOIN tasks ON tasks.id = task_versions.task_id
            JOIN systems ON systems.id = trials.system_id
            {where}
            ORDER BY trials.started_at
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    payload = [dict(row) for row in rows]
    if as_json:
        console.print_json(json.dumps(payload))
        return

    columns = ["trial_key", "task", "system", "strict_pass", "coldstart_pass", "runtime_sec", "cost_usd", "is_infra_exception"]
    table = Table(title="Trials")
    for column in columns:
        table.add_column(column)
    for row in payload:
        table.add_row(*(str(row[c]) if row[c] is not None else "" for c in columns))
    console.print(table)


@results_app.command("show-run")
def show_run(
    run_id: str = typer.Argument(..., help="Run key (Harbor job directory name)."),
    db: Path = DB_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of tables."),
) -> None:
    """Show a single evaluation run and its trials."""
    conn = db_module.connect(db)
    try:
        run_row = conn.execute("SELECT * FROM runs WHERE run_key = ?", (run_id,)).fetchone()
        if run_row is None:
            error_console.print(f"[red]No run found with key '{run_id}'[/red]")
            raise typer.Exit(1)
        trial_rows = conn.execute(
            """
            SELECT trials.trial_key AS trial_key, tasks.name AS task,
                   systems.system_key AS system, trials.strict_pass AS strict_pass,
                   trials.coldstart_pass AS coldstart_pass, trials.runtime_sec AS runtime_sec,
                   trials.cost_usd AS cost_usd, trials.is_infra_exception AS is_infra_exception
            FROM trials
            JOIN task_versions ON task_versions.id = trials.task_version_id
            JOIN tasks ON tasks.id = task_versions.task_id
            JOIN systems ON systems.id = trials.system_id
            WHERE trials.run_id = ?
            ORDER BY trials.started_at
            """,
            (run_row["id"],),
        ).fetchall()
    finally:
        conn.close()

    if as_json:
        console.print_json(json.dumps({"run": dict(run_row), "trials": [dict(r) for r in trial_rows]}))
        return

    console.print(f"[bold]Run:[/bold] {run_row['run_key']} (job_uuid={run_row['job_uuid']})")
    console.print(
        f"started_at={run_row['started_at']} finished_at={run_row['finished_at']} "
        f"n_total_trials={run_row['n_total_trials']} n_errored_trials={run_row['n_errored_trials']}"
    )
    table = Table(title=f"Trials in {run_id}")
    columns = ["trial_key", "task", "system", "strict_pass", "coldstart_pass", "runtime_sec", "cost_usd", "is_infra_exception"]
    for column in columns:
        table.add_column(column)
    for row in trial_rows:
        row_dict = dict(row)
        table.add_row(*(str(row_dict[c]) if row_dict[c] is not None else "" for c in columns))
    console.print(table)


@reports_app.command("task")
def report_task(
    task: str = typer.Option(..., "--task", help="Task name (e.g. artifact-vault-recovery)."),
    system: str = typer.Option(..., "--system", help="System key: <model>__<agent>."),
    visibility: str = typer.Option(..., "--visibility", help="'public' or 'private'."),
    format: str = typer.Option("markdown", "--format", help="'markdown' or 'json'."),
    db: Path = DB_OPTION,
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Output file path. Defaults to reports/ (public) or results/private/ (private)."
    ),
) -> None:
    """Generate a public or private task/system report."""
    conn = db_module.connect(db)
    try:
        report = build_report(conn, task=task, system=system, visibility=visibility)
    except ValueError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        conn.close()

    if format == "json":
        rendered = render_json(report)
        extension = "json"
    elif format == "markdown":
        rendered = render_markdown(report)
        extension = "md"
    else:
        error_console.print("[red]--format must be 'markdown' or 'json'[/red]")
        raise typer.Exit(1)

    if output is None:
        base_dir = Path("reports") if visibility == "public" else Path("results/private")
        safe_system = system.replace("/", "-")
        output = base_dir / f"{task}__{safe_system}.{visibility}.{extension}"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    console.print(f"Wrote {visibility} report: {output}")


__all__ = ["results_app", "reports_app"]
