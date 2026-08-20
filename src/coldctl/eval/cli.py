"""Typer commands for the ColdStart automated evaluation runner."""

from __future__ import annotations

import json as json_module
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from coldctl.eval import manifest as manifest_module
from coldctl.eval import orchestrator
from coldctl.eval.config import ConfigError, load_config
from coldctl.eval.harbor_runner import SubprocessHarborRunner
from coldctl.eval.planner import DirtyWorktreeError, build_plan
from coldctl.eval.progress import ProgressRenderer
from coldctl.task_validation import find_missing_task_files

eval_app = typer.Typer(help="Plan and run automated, provider-agnostic evaluations.", no_args_is_help=True)

console = Console()
error_console = Console(stderr=True)

DEFAULT_COLDSTART_DIR = Path(".coldstart")


def _phase1_db_path(coldstart_dir: Path) -> Path:
    return coldstart_dir / "results.db"


def _fail(problems: list[str], *, as_json: bool) -> None:
    if as_json:
        console.print_json(json_module.dumps({"valid": False, "problems": problems}))
    else:
        error_console.print("[red]Invalid[/red]")
        for problem in problems:
            error_console.print(f"  - {problem}")
    raise typer.Exit(1)


@eval_app.command("validate")
def eval_validate(
    config_path: Path = typer.Argument(..., help="Path to an evaluation configuration YAML file."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
) -> None:
    """Read-only, zero-cost validation. Never creates a run or calls Harbor."""
    problems: list[str] = []
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _fail([str(exc)], as_json=as_json)
        return

    repo_root = Path.cwd()
    for task in config.tasks:
        task_dir = repo_root / task
        if not task_dir.is_dir():
            problems.append(f"task path does not exist: {task}")
            continue
        for missing in find_missing_task_files(task_dir):
            problems.append(f"task '{task}' is missing required file: {missing}")

    problems.extend(orchestrator.preflight_environment_checks(config))

    if problems:
        _fail(problems, as_json=as_json)
        return

    if as_json:
        console.print_json(json_module.dumps({"valid": True, "problems": []}))
    else:
        console.print(f"[green]Configuration is valid:[/green] {config_path}")


def _render_plan(plan, config, *, as_json: bool) -> None:
    if as_json:
        console.print_json(json_module.dumps(plan.to_dict()))
        return

    console.print(f"[bold]Evaluation:[/bold] {config.id} ({config.status})")
    console.print(f"Config hash: {plan.config_hash}")
    console.print(f"Git commit: {plan.git_commit} (dirty={plan.git_dirty}, unverified={plan.unverified})")
    console.print(f"Benchmark version: {plan.benchmark_version}")
    console.print(f"Total planned trials: {plan.total_planned_trials}")

    estimate = plan.cost_estimate
    if estimate.total_usd is not None:
        console.print(f"Estimated total cost: ${estimate.total_usd:.7f} (source: {estimate.source})")
    else:
        console.print(f"Estimated total cost: unavailable (source: {estimate.source})")
    console.print(f"Configured max budget: ${config.execution.max_budget_usd:.2f}")

    table = Table(title="Planned trials")
    for column in ["trial_id", "task_name", "system_key", "attempt"]:
        table.add_column(column)
    for trial in plan.trials:
        table.add_row(trial.trial_id, trial.task_name, trial.system_key, str(trial.attempt))
    console.print(table)


@eval_app.command("plan")
def eval_plan(
    config_path: Path = typer.Argument(..., help="Path to an evaluation configuration YAML file."),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Allow a dirty/unverifiable worktree (marks the plan unverified)."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
    coldstart_dir: Path = typer.Option(DEFAULT_COLDSTART_DIR, "--coldstart-dir", hidden=True),
) -> None:
    """Produce a deterministic plan. Never calls Harbor; never spends money."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    repo_root = Path.cwd()
    try:
        plan = build_plan(
            config,
            repo_root=repo_root,
            phase1_db_path=_phase1_db_path(coldstart_dir),
            allow_dirty=allow_dirty,
        )
    except DirtyWorktreeError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    _render_plan(plan, config, as_json=as_json)


@eval_app.command("run")
def eval_run(
    config_path: Path = typer.Argument(..., help="Path to an evaluation configuration YAML file."),
    yes: bool = typer.Option(False, "--yes", help="Confirm non-interactive execution. Without this, only the plan is shown."),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Allow a dirty/unverifiable worktree for a development run."),
    acknowledge_unestimated_cost: bool = typer.Option(
        False,
        "--acknowledge-unestimated-cost",
        help="Required together with --yes when no cost estimate is available.",
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Show sanitized Harbor output in the progress display."),
    coldstart_dir: Path = typer.Option(DEFAULT_COLDSTART_DIR, "--coldstart-dir", hidden=True),
) -> None:
    """Execute a plan trial-by-trial, ingesting and reporting as it goes."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    repo_root = Path.cwd()
    phase1_db_path = _phase1_db_path(coldstart_dir)
    try:
        plan = build_plan(config, repo_root=repo_root, phase1_db_path=phase1_db_path, allow_dirty=allow_dirty)
    except DirtyWorktreeError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if not yes:
        _render_plan(plan, config, as_json=False)
        console.print(
            "\n[yellow]No trials were run and no money was spent.[/yellow] "
            "Pass --yes to execute this plan."
        )
        raise typer.Exit(0)

    try:
        with ProgressRenderer(console=console, verbose=verbose) as renderer:
            outcome = orchestrator.run_evaluation(
                config,
                repo_root=repo_root,
                coldstart_dir=coldstart_dir,
                phase1_db_path=phase1_db_path,
                allow_dirty=allow_dirty,
                budget_ack=acknowledge_unestimated_cost,
                harbor_runner=SubprocessHarborRunner(),
                progress_callback=renderer,
            )
    except orchestrator.PreflightError as exc:
        error_console.print("[red]Preflight checks failed:[/red]")
        for problem in exc.problems:
            error_console.print(f"  - {problem}")
        raise typer.Exit(1) from exc
    except (orchestrator.BudgetExceededError, orchestrator.BudgetAcknowledgmentRequiredError) as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except orchestrator.RunInterrupted as exc:
        error_console.print(
            f"\n[yellow]Run {exc.state.run_id} interrupted and paused.[/yellow] "
            f"Resume with: coldctl eval resume {exc.state.run_id} --yes"
        )
        raise typer.Exit(130) from exc

    _print_run_outcome(outcome)


def _print_run_outcome(outcome: orchestrator.RunOutcome) -> None:
    state = outcome.state
    console.print(f"[bold]Run {outcome.run_id}: {state.status}[/bold]")
    passes = sum(1 for t in state.trials.values() if t.status == "passed")
    failures = sum(1 for t in state.trials.values() if t.status == "failed")
    infra_exhausted = sum(1 for t in state.trials.values() if t.status == "infra_invalid_exhausted")
    console.print(
        f"Trials -- planned: {len(state.trials)}  finished: {len(state.completed_trial_ids)}  "
        f"pending: {len(state.pending_trial_ids)}"
    )
    # "Finished" (above) includes infra-exhausted trials as filled slots for
    # progress purposes, but they are never scored model outcomes -- keep
    # scored passes/failures separate from infra-only exhaustion here so a
    # terminal infra failure can never be read as a completed model trial.
    console.print(
        f"Scored: {passes + failures} (passed: {passes}  failed: {failures})  "
        f"Infra-failed (retries exhausted): {infra_exhausted}  "
        f"Invalid infra attempts: {state.invalid_infrastructure_attempts}"
    )
    console.print(f"Accumulated cost: ${state.actual_cost_usd:.7f}")
    if state.private_report.generated or state.public_report.generated:
        console.print(
            f"Reports -- private: {state.private_report.path or 'n/a'}; "
            f"public: {state.public_report.path or 'n/a'}"
        )
    if state.status == "paused":
        console.print(f"Resume with: coldctl eval resume {outcome.run_id} --yes")
    exit_code = 0 if state.status == "completed" else 1
    if exit_code:
        raise typer.Exit(exit_code)


@eval_app.command("resume")
def eval_resume(
    run_id: str = typer.Argument(..., help="Run ID to resume (as printed by `eval run`/`eval status`)."),
    yes: bool = typer.Option(False, "--yes", help="Required to resume execution."),
    acknowledge_unestimated_cost: bool = typer.Option(
        False, "--acknowledge-unestimated-cost", help="Required when no cost estimate was available."
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Show sanitized Harbor output in the progress display."),
    coldstart_dir: Path = typer.Option(DEFAULT_COLDSTART_DIR, "--coldstart-dir", hidden=True),
) -> None:
    """Resume an interrupted/paused run from its frozen manifest."""
    if not yes:
        error_console.print("[red]--yes is required to resume a run (this may spend money).[/red]")
        raise typer.Exit(1)

    run_dir = manifest_module.run_dir_for(coldstart_dir, run_id)
    try:
        manifest_dict = manifest_module.read_manifest(run_dir)
    except FileNotFoundError as exc:
        error_console.print(f"[red]No such run: {run_id}[/red]")
        raise typer.Exit(1) from exc

    config_path = manifest_dict.get("config_path")
    if not config_path:
        error_console.print("[red]This run's manifest does not record a config path; cannot resume.[/red]")
        raise typer.Exit(1)
    try:
        config = load_config(Path(config_path))
    except ConfigError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    repo_root = Path.cwd()
    phase1_db_path = _phase1_db_path(coldstart_dir)
    try:
        with ProgressRenderer(console=console, verbose=verbose) as renderer:
            outcome = orchestrator.resume_evaluation(
                run_id,
                config,
                repo_root=repo_root,
                coldstart_dir=coldstart_dir,
                phase1_db_path=phase1_db_path,
                budget_ack=acknowledge_unestimated_cost,
                harbor_runner=SubprocessHarborRunner(),
                progress_callback=renderer,
            )
    except orchestrator.ResumeDriftError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except orchestrator.PreflightError as exc:
        error_console.print("[red]Preflight checks failed:[/red]")
        for problem in exc.problems:
            error_console.print(f"  - {problem}")
        raise typer.Exit(1) from exc
    except (orchestrator.BudgetExceededError, orchestrator.BudgetAcknowledgmentRequiredError) as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except orchestrator.RunInterrupted as exc:
        error_console.print(
            f"\n[yellow]Run {exc.state.run_id} interrupted and paused again.[/yellow] "
            f"Resume with: coldctl eval resume {exc.state.run_id} --yes"
        )
        raise typer.Exit(130) from exc

    _print_run_outcome(outcome)


@eval_app.command("status")
def eval_status(
    run_id: str = typer.Argument(..., help="Run ID."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of text."),
    coldstart_dir: Path = typer.Option(DEFAULT_COLDSTART_DIR, "--coldstart-dir", hidden=True),
) -> None:
    """Show a run's current status."""
    run_dir = manifest_module.run_dir_for(coldstart_dir, run_id)
    try:
        manifest_dict = manifest_module.read_manifest(run_dir)
        state = manifest_module.read_state(run_dir)
    except FileNotFoundError as exc:
        error_console.print(f"[red]No such run: {run_id}[/red]")
        raise typer.Exit(1) from exc

    passes = sum(1 for t in state.trials.values() if t.status == "passed")
    failures = sum(1 for t in state.trials.values() if t.status == "failed")
    invalid = state.invalid_infrastructure_attempts
    remaining_budget = manifest_dict["configured_budget_usd"] - state.actual_cost_usd

    try:
        elapsed_sec = (
            datetime.fromisoformat(state.updated_at) - datetime.fromisoformat(state.created_at)
        ).total_seconds()
    except ValueError:
        elapsed_sec = None

    system_summaries = manifest_dict.get("systems", [])
    payload = {
        "run_id": run_id,
        "status": state.status,
        "systems": system_summaries,
        "benchmark_version": manifest_dict.get("benchmark_version"),
        "git_commit": manifest_dict.get("git_commit"),
        "planned_trials": manifest_dict.get("expected_trial_count"),
        "completed_trials": len(state.completed_trial_ids),
        "pending_trials": len(state.pending_trial_ids),
        "passes": passes,
        "failures": failures,
        "invalid_infrastructure_attempts": invalid,
        "accumulated_cost_usd": state.actual_cost_usd,
        "configured_budget_usd": manifest_dict["configured_budget_usd"],
        "remaining_budget_usd": remaining_budget,
        "elapsed_sec": elapsed_sec,
        "last_event": state.last_event,
        "private_report": {"generated": state.private_report.generated, "path": state.private_report.path},
        "public_report": {"generated": state.public_report.generated, "path": state.public_report.path},
    }

    if as_json:
        console.print_json(json_module.dumps(payload))
        return

    console.print(f"[bold]Run {run_id}: {state.status}[/bold]")
    for system in system_summaries:
        console.print(f"  Model: {system['model']}  Agent: {system['agent']}")
    console.print(f"Benchmark version: {payload['benchmark_version']}  Git commit: {payload['git_commit']}")
    console.print(
        f"Trials -- planned: {payload['planned_trials']}  completed: {payload['completed_trials']}  "
        f"pending: {payload['pending_trials']}"
    )
    console.print(f"Passes: {passes}  Failures: {failures}  Invalid infra attempts: {invalid}")
    console.print(
        f"Cost -- accumulated: ${state.actual_cost_usd:.7f}  "
        f"budget: ${payload['configured_budget_usd']:.2f}  remaining: ${remaining_budget:.7f}"
    )
    console.print(f"Elapsed: {elapsed_sec}s" if elapsed_sec is not None else "Elapsed: unknown")
    console.print(f"Last event: {state.last_event}")
    console.print(
        f"Reports -- private: {state.private_report.generated} ({state.private_report.path}); "
        f"public: {state.public_report.generated} ({state.public_report.path})"
    )


@eval_app.command("regenerate-reports")
def eval_regenerate_reports(
    run_id: str = typer.Argument(..., help="Run ID to regenerate Phase 1 reports for."),
    coldstart_dir: Path = typer.Option(DEFAULT_COLDSTART_DIR, "--coldstart-dir", hidden=True),
) -> None:
    """Regenerate this run's public/private reports from its already-ingested
    trials, without executing Harbor. Scoped to exactly the trials this run
    produced -- never all history for the same task/system."""
    try:
        outcome = orchestrator.regenerate_reports(
            run_id,
            repo_root=Path.cwd(),
            coldstart_dir=coldstart_dir,
            phase1_db_path=_phase1_db_path(coldstart_dir),
        )
    except FileNotFoundError as exc:
        error_console.print(f"[red]No such run: {run_id}[/red]")
        raise typer.Exit(1) from exc
    except orchestrator.ReportMembershipError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if not outcome.private_report.generated and not outcome.public_report.generated:
        console.print(
            "[yellow]No ingested trials found for this run; no report generated.[/yellow]"
        )
        raise typer.Exit(1)

    console.print(f"Regenerated reports for run {run_id}")
    console.print(f"  private: {outcome.private_report.generated} ({outcome.private_report.path})")
    console.print(f"  public:  {outcome.public_report.generated} ({outcome.public_report.path})")


__all__ = ["eval_app"]
