"""Live evaluation progress: event stream + renderers.

The orchestrator emits :class:`ProgressEvent` objects as plain data through
an injected callback -- it never renders anything itself. This keeps the
orchestration loop fully testable without a terminal: a test can inject
``events.append`` and assert on the event stream directly. Rendering (Rich
``Live`` display for interactive terminals, a periodic heartbeat line
otherwise) lives entirely in :class:`ProgressRenderer`, used only by the CLI.

Overall completion percentage is always derived from
``completed_trials / planned_trials`` -- never from elapsed wall-clock time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from rich.console import Console

from coldctl.eval.redact import redact_text

PHASES = (
    "preparing",
    "launching_harbor",
    "agent_running",
    "verifying",
    "ingesting",
    "generating_reports",
    "completed",
)

_PHASE_LABELS = {
    "preparing": "preparing",
    "launching_harbor": "launching Harbor",
    "agent_running": "agent running",
    "verifying": "verifying",
    "ingesting": "ingesting",
    "generating_reports": "generating reports",
    "completed": "completed",
}


@dataclass
class ProgressEvent:
    run_id: str
    completed_trials: int
    planned_trials: int
    current_trial_number: int
    task_name: str
    model: str
    agent: str
    phase: str
    elapsed_sec: float
    harbor_status: str  # "not_started" | "active" | "exited"
    stdout_tail: str = ""

    @property
    def phase_label(self) -> str:
        return _PHASE_LABELS.get(self.phase, self.phase)

    @property
    def overall_percent(self) -> float:
        """Real completion percentage from completed/planned trial counts
        only -- never a time-based estimate."""
        if self.planned_trials <= 0:
            return 0.0
        return 100.0 * self.completed_trials / self.planned_trials

    def format_line(self) -> str:
        """A single, redacted, secret-free heartbeat line."""
        elapsed = _format_elapsed(self.elapsed_sec)
        line = (
            f"[{self.run_id}] overall {self.completed_trials}/{self.planned_trials} "
            f"({self.overall_percent:.0f}%) -- trial {self.current_trial_number}/{self.planned_trials} "
            f"task={self.task_name} model={self.model} agent={self.agent} "
            f"phase={self.phase_label} elapsed={elapsed} harbor={self.harbor_status}"
        )
        return redact_text(line)


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


class ProgressRenderer:
    """Renders a :class:`ProgressEvent` stream.

    Interactive terminals get a Rich ``Live`` display updated on every
    event; non-interactive terminals (CI, redirected output, piped stdout)
    get a concise heartbeat line printed at most once per
    ``heartbeat_interval_sec`` (always on a phase change, though, so
    transitions are never missed) -- Rich's live-updating renderer would
    otherwise spam a plain log with redrawn frames.
    """

    def __init__(
        self,
        *,
        console: Console | None = None,
        interactive: bool | None = None,
        heartbeat_interval_sec: float = 5.0,
        clock_fn: Callable[[], float] = time.monotonic,
        verbose: bool = False,
    ) -> None:
        self._console = console or Console()
        self._interactive = interactive if interactive is not None else bool(self._console.is_terminal)
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._clock_fn = clock_fn
        self._verbose = verbose
        self._live = None
        self._last_heartbeat_at: float | None = None
        self._last_phase: str | None = None

    def __enter__(self) -> "ProgressRenderer":
        if self._interactive:
            from rich.live import Live

            self._live = Live(console=self._console, refresh_per_second=4, transient=False)
            self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._live is not None:
            self._live.__exit__(exc_type, exc_val, exc_tb)
            self._live = None

    def __call__(self, event: ProgressEvent) -> None:
        if self._interactive and self._live is not None:
            self._live.update(self._render_panel(event))
            return
        self._maybe_heartbeat(event)

    def _render_panel(self, event: ProgressEvent):
        from rich.panel import Panel
        from rich.table import Table

        table = Table.grid(padding=(0, 1))
        table.add_column()
        table.add_column()
        table.add_row("ColdStart run:", redact_text(event.run_id))
        table.add_row(
            "Overall:", f"{event.completed_trials}/{event.planned_trials} completed ({event.overall_percent:.0f}%)"
        )
        table.add_row("Current:", f"trial {event.current_trial_number}/{event.planned_trials}")
        table.add_row("Task:", redact_text(event.task_name))
        table.add_row("System:", redact_text(f"{event.model} + {event.agent}"))
        table.add_row("Phase:", event.phase_label)
        table.add_row("Elapsed:", _format_elapsed(event.elapsed_sec))
        table.add_row("Harbor:", event.harbor_status)
        if self._verbose and event.stdout_tail:
            table.add_row("Output:", redact_text(event.stdout_tail.strip().splitlines()[-1] if event.stdout_tail.strip() else ""))
        return Panel(table, title="ColdStart evaluation progress")

    def _maybe_heartbeat(self, event: ProgressEvent) -> None:
        now = self._clock_fn()
        phase_changed = event.phase != self._last_phase
        due = self._last_heartbeat_at is None or (now - self._last_heartbeat_at) >= self._heartbeat_interval_sec
        if not (phase_changed or due):
            return
        line = event.format_line()
        if self._verbose and event.stdout_tail.strip():
            last_line = event.stdout_tail.strip().splitlines()[-1]
            line = f"{line} | harbor: {redact_text(last_line)}"
        # markup=False: this is pre-formatted, already-redacted plain text
        # (which may legitimately contain literal '[' characters, e.g. in a
        # run ID) and must never be interpreted as Rich style markup.
        self._console.print(line, markup=False)
        self._last_heartbeat_at = now
        self._last_phase = event.phase
