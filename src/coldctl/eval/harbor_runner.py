"""Harbor process invocation: one `harbor run` per planned trial.

Running Harbor once per trial (rather than using its own `--n-attempts`
multi-trial-per-job support) trades a few extra process launches for exact,
deterministic job-directory discovery, per-trial budgeting, and safe
resumability -- we always know in advance exactly which directory a given
trial's job will land in, because we choose ``--job-name``/``--jobs-dir``
ourselves.

Argument vectors are always built as lists and passed to ``subprocess.Popen``
directly -- never through a shell -- so nothing here is vulnerable to shell
injection from configuration content.

The real runner is *pollable*: rather than blocking silently on
``subprocess.run`` until Harbor exits (which could be many minutes with no
visible feedback at all), it launches Harbor with stdout/stderr redirected
to files (never pipes -- a pipe can deadlock once its OS buffer fills if
nobody is draining it; a file has no such limit) and polls the process at a
short interval, invoking an optional ``on_progress`` callback each tick so a
caller can render live feedback. The complete captured stdout/stderr is
still returned at the end for private logging, unabridged.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

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


@dataclass
class ProgressUpdate:
    """One poll tick while a Harbor invocation is in flight."""

    elapsed_sec: float
    harbor_alive: bool
    stdout_tail: str = ""


OnProgress = Callable[[ProgressUpdate], None]


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
        self,
        *,
        trial: TrialSpec,
        job_name: str,
        jobs_dir: Path,
        on_progress: OnProgress | None = None,
    ) -> HarborInvocationResult: ...


class SubprocessHarborRunner:
    """Real Harbor runner: invokes the installed `harbor` CLI via subprocess
    with an explicit argument array (never a shell string), inheriting the
    parent process environment (so API keys already present there reach
    Harbor/the model provider) without ever placing a credential on the
    command line.

    ``poll_interval_sec``/``sleep_fn``/``clock_fn`` are overridable so tests
    can exercise the polling loop with an injected clock/sleeper instead of
    real wall-clock delays.
    """

    def __init__(
        self,
        *,
        timeout_sec: float | None = None,
        poll_interval_sec: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout_sec = timeout_sec
        self._poll_interval_sec = poll_interval_sec
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn

    def run_trial(
        self,
        *,
        trial: TrialSpec,
        job_name: str,
        jobs_dir: Path,
        on_progress: OnProgress | None = None,
    ) -> HarborInvocationResult:
        invocation = build_harbor_invocation(trial, job_name=job_name, jobs_dir=jobs_dir)
        jobs_dir_path = Path(jobs_dir)
        jobs_dir_path.mkdir(parents=True, exist_ok=True)
        stdout_path = jobs_dir_path / f".{job_name}.stdout.tmp"
        stderr_path = jobs_dir_path / f".{job_name}.stderr.tmp"

        start = self._clock_fn()
        timed_out = False
        with stdout_path.open("w+") as out_handle, stderr_path.open("w+") as err_handle:
            process = subprocess.Popen(
                invocation.argv,
                stdout=out_handle,
                stderr=err_handle,
                text=True,
                env=None,  # inherit the parent environment verbatim; never rebuilt here
            )
            try:
                while True:
                    returncode = process.poll()
                    elapsed = self._clock_fn() - start
                    if on_progress is not None:
                        on_progress(
                            ProgressUpdate(
                                elapsed_sec=elapsed,
                                harbor_alive=returncode is None,
                                stdout_tail=_tail(out_handle),
                            )
                        )
                    if returncode is not None:
                        break
                    if self._timeout_sec is not None and elapsed >= self._timeout_sec:
                        timed_out = True
                        process.kill()
                        process.wait()
                        break
                    self._sleep_fn(self._poll_interval_sec)
            except KeyboardInterrupt:
                # Never leave an orphaned Harbor process (and its spend)
                # running in the background after an interrupt.
                process.kill()
                process.wait()
                raise
            out_handle.seek(0)
            stdout_text = out_handle.read()
            err_handle.seek(0)
            stderr_text = err_handle.read()

        returncode = -1 if timed_out else (process.returncode if process.returncode is not None else -1)
        if timed_out:
            stderr_text += "\n[coldctl] harbor invocation timed out"

        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)

        return HarborInvocationResult(
            returncode=returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            job_dir=invocation.job_dir,
            argv_redacted=redact_argv(invocation.argv),
        )


def _tail(handle, max_chars: int = 2000) -> str:
    """Best-effort recent output from an open, still-growing file handle,
    without disturbing the caller's own read position semantics on Windows;
    used only for progress/phase heuristics and --verbose display."""
    try:
        position = handle.tell()
        handle.seek(0)
        content = handle.read()
        handle.seek(position)
    except OSError:
        return ""
    return content[-max_chars:]
