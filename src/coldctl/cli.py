"""Thin ColdStart wrapper around Harbor."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

from coldctl import __version__
from coldctl.results.cli import reports_app, results_app

app = typer.Typer(
    name="coldctl",
    help="Author, validate, and run ColdStart benchmark tasks.",
    no_args_is_help=True,
)
task_app = typer.Typer(help="Create and manage ColdStart tasks.", no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(results_app, name="results")
app.add_typer(reports_app, name="reports")


def _require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise typer.BadParameter(
            f"Required command '{command}' was not found on PATH."
        )


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


@app.command()
def version() -> None:
    """Print the installed ColdStart CLI version."""
    typer.echo(__version__)


@app.command()
def doctor(
    skip_docker: bool = typer.Option(False, help="Skip the Docker daemon check."),
) -> None:
    """Check whether the local authoring workstation is ready."""
    commands = ["git", "harbor"]
    if not skip_docker:
        commands.append("docker")

    failures: list[str] = []
    for command in commands:
        if shutil.which(command) is None:
            failures.append(f"{command}: not found")
        else:
            typer.echo(f"{command}: found")

    if not skip_docker and "docker: not found" not in failures:
        probe = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode != 0:
            failures.append("docker: daemon is unavailable")
        else:
            typer.echo("docker daemon: available")

    if failures:
        for failure in failures:
            typer.echo(f"ERROR: {failure}", err=True)
        raise typer.Exit(1)

    typer.echo("ColdStart workstation check passed.")


@task_app.command("init")
def task_init(
    name: str = typer.Argument(..., help="Lowercase task identifier."),
    destination: Path = typer.Option(
        Path("benchmark/sample-tasks"),
        "--destination",
        "-d",
        help="Directory in which Harbor should create the task.",
    ),
) -> None:
    """Create a native Harbor task in the sample-task directory."""
    _require_command("harbor")
    if not name or name != name.lower() or " " in name:
        raise typer.BadParameter("Use a lowercase, hyphenated task identifier.")

    destination.mkdir(parents=True, exist_ok=True)
    target = destination / name
    if target.exists():
        raise typer.BadParameter(f"Task already exists: {target}")

    _run(["harbor", "task", "init", name], cwd=destination)
    typer.echo(f"Created task scaffold: {target}")


@app.command()
def validate(
    task_path: Path = typer.Argument(..., exists=True, file_okay=False),
) -> None:
    """Validate the minimum ColdStart and Harbor task structure."""
    required = [
        "instruction.md",
        "task.toml",
        "solution/solve.sh",
        "tests/test.sh",
    ]
    missing = [relative for relative in required if not (task_path / relative).is_file()]

    environment_options = [
        "environment/Dockerfile",
        "environment/docker-compose.yaml",
        "environment/docker-compose.yml",
    ]
    if not any((task_path / option).is_file() for option in environment_options):
        missing.append("environment/Dockerfile or docker-compose.yaml")

    if missing:
        typer.echo("Task structure failed validation:", err=True)
        for relative in missing:
            typer.echo(f"  - missing {relative}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Task structure is valid: {task_path}")


@app.command()
def oracle(
    task_path: Path = typer.Argument(..., exists=True, file_okay=False),
    runs: int = typer.Option(1, min=1, max=20, help="Number of consecutive Oracle runs."),
) -> None:
    """Run a task with Harbor's Oracle agent."""
    _require_command("harbor")
    for index in range(1, runs + 1):
        typer.echo(f"Oracle run {index}/{runs}")
        _run(["harbor", "run", "-p", str(task_path), "-a", "oracle"])
    typer.echo(f"Completed {runs} Oracle run(s).")


@app.command()
def view(
    jobs_path: Path = typer.Argument(Path("jobs"), file_okay=False),
) -> None:
    """Open Harbor's local job and trajectory viewer."""
    _require_command("harbor")
    _run(["harbor", "view", str(jobs_path)])
