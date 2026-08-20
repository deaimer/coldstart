"""Harbor process invocation: one `harbor run` per planned trial.

Running Harbor once per trial (rather than using its own `--n-attempts`
multi-trial-per-job support) trades a few extra process launches for exact,
deterministic job-directory discovery, per-trial budgeting, and safe
resumability -- we always know in advance exactly which directory a given
trial's job will land in, because we choose ``--job-name``/``--jobs-dir``
ourselves.

Argument vectors are always built as lists and passed to ``subprocess.run``
directly -- never through a shell -- so nothing here is vulnerable to shell
injection from configuration content.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from coldctl.eval.planner import TrialSpec
from coldctl.eval.redact import redact_argv


@dataclass
class HarborInvocation:
    argv: list[str]
    job_dir: Path


@dataclass
class HarborInvocationResult:
    returncode: int
    stdout: str
    stderr: str
    job_dir: Path
    argv_redacted: list[str]


def _format_kwarg_value(value: object) -> str:
    """Matches Harbor's own `--ak key=value` parsing (JSON-first, with
    True/False/None literal fallbacks): JSON-encoding every value round-trips
    correctly through Harbor's `json.loads`-first parser."""
    return json.dumps(value)


def build_harbor_invocation(
    trial: TrialSpec, *, job_name: str, jobs_dir: Path, extra_args: list[str] | None = None
) -> HarborInvocation:
    argv = [
        "harbor",
        "run",
        "-p",
        trial.task_path,
        "-a",
        trial.agent,
        "-m",
        trial.model,
        "-e",
        trial.environment,
        "--job-name",
        job_name,
        "--jobs-dir",
        str(jobs_dir),
        "-y",
    ]
    for key in sorted(trial.agent_kwargs):
        argv.extend(["--ak", f"{key}={_format_kwarg_value(trial.agent_kwargs[key])}"])
    if extra_args:
        argv.extend(extra_args)
    return HarborInvocation(argv=argv, job_dir=Path(jobs_dir) / job_name)


class HarborRunner(Protocol):
    """Substitutable interface so tests never need a real Harbor/Docker/API."""

    def run_trial(
        self, *, trial: TrialSpec, job_name: str, jobs_dir: Path
    ) -> HarborInvocationResult: ...


class SubprocessHarborRunner:
    """Real Harbor runner: invokes the installed `harbor` CLI via subprocess
    with an explicit argument array (never a shell string), inheriting the
    parent process environment (so API keys already present there reach
    Harbor/the model provider) without ever placing a credential on the
    command line."""

    def __init__(self, *, timeout_sec: float | None = None) -> None:
        self._timeout_sec = timeout_sec

    def run_trial(
        self, *, trial: TrialSpec, job_name: str, jobs_dir: Path
    ) -> HarborInvocationResult:
        invocation = build_harbor_invocation(trial, job_name=job_name, jobs_dir=jobs_dir)
        try:
            completed = subprocess.run(
                invocation.argv,
                capture_output=True,
                text=True,
                check=False,
                env=None,  # inherit the parent environment verbatim; never rebuilt here
                timeout=self._timeout_sec,
            )
            returncode, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = -1
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + "\n[coldctl] harbor invocation timed out"
        return HarborInvocationResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            job_dir=invocation.job_dir,
            argv_redacted=redact_argv(invocation.argv),
        )
