"""Rich CLI dashboard — Phase 6.

Provides a live terminal display that shows the status of each pipeline
step and each worker in real time.

Usage
-----
Instantiate ``TurbineUI`` as a context manager.  The Manager calls the
``on_*`` event methods as it progresses; the UI updates the display.

    with TurbineUI(title="my project") as ui:
        manager.ui = ui
        await manager.run()

When ``TurbineUI`` is used as a context manager it starts ``rich.Live``
automatically.  Outside a ``with`` block the on_* methods are no-ops so
the rest of the codebase does not need to guard against a missing UI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Worker status
# ---------------------------------------------------------------------------

class WorkerStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    APPROVED  = "approved"
    CONFLICT  = "conflict"
    FAILED    = "failed"
    REPAIR    = "repair"


_STATUS_STYLE: dict[WorkerStatus, str] = {
    WorkerStatus.PENDING:  "dim",
    WorkerStatus.RUNNING:  "bold cyan",
    WorkerStatus.APPROVED: "bold green",
    WorkerStatus.CONFLICT: "bold yellow",
    WorkerStatus.FAILED:   "bold red",
    WorkerStatus.REPAIR:   "bold magenta",
}

_STATUS_ICON: dict[WorkerStatus, str] = {
    WorkerStatus.PENDING:  "○",
    WorkerStatus.RUNNING:  "◌",
    WorkerStatus.APPROVED: "✓",
    WorkerStatus.CONFLICT: "⚡",
    WorkerStatus.FAILED:   "✗",
    WorkerStatus.REPAIR:   "↺",
}


@dataclass
class WorkerState:
    ticket_id: str
    description: str
    status: WorkerStatus = WorkerStatus.PENDING
    attempt: int = 0
    detail: str = ""
    elapsed: float = 0.0
    token_chars: int = 0   # Phase 16: running character count from streaming
    _start: float = field(default_factory=time.monotonic, repr=False)

    def start(self) -> None:
        self._start = time.monotonic()
        self.status = WorkerStatus.RUNNING

    def finish(self, success: bool) -> None:
        self.elapsed = time.monotonic() - self._start
        self.status = WorkerStatus.APPROVED if success else WorkerStatus.FAILED

    def tick_elapsed(self) -> None:
        if self.status == WorkerStatus.RUNNING:
            self.elapsed = time.monotonic() - self._start


# ---------------------------------------------------------------------------
# Pipeline step tracker
# ---------------------------------------------------------------------------

class PipelineStep(str, Enum):
    DISCOVER    = "1 · Discover"
    PREPROCESS  = "2 · Preprocess"
    INVESTIGATE = "3 · Investigate"
    DELEGATE    = "4 · Delegate"
    COMMIT      = "5 · Commit & Verify"
    DONE        = "Done"


_STEP_ORDER = list(PipelineStep)


# ---------------------------------------------------------------------------
# TurbineUI
# ---------------------------------------------------------------------------

class TurbineUI:
    """Live Rich dashboard for the Turbine pipeline.

    All ``on_*`` methods are safe to call even when the UI is not active
    (i.e. when ``enabled=False`` or outside a ``with`` block); they become
    silent no-ops so callers never need to guard.

    Parameters
    ----------
    title:
        Project path or name shown in the header.
    enabled:
        Set to ``False`` to suppress all output (e.g. in tests or when
        stdout is not a TTY).
    refresh_per_second:
        How often ``rich.Live`` redraws the display.
    """

    def __init__(
        self,
        title: str = "",
        enabled: bool = True,
        refresh_per_second: int = 8,
    ) -> None:
        self.title = title
        self.enabled = enabled
        self._refresh = refresh_per_second

        self._console = Console()
        self._live: Live | None = None
        self._active = False

        # Pipeline state
        self._current_step: PipelineStep = PipelineStep.DISCOVER
        self._step_messages: dict[PipelineStep, str] = {}

        # Worker states keyed by ticket_id
        self._workers: dict[str, WorkerState] = {}

        # Overall progress bar (steps)
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=self._console,
            transient=False,
        )
        self._step_task: TaskID = self._progress.add_task(
            "Pipeline", total=len(_STEP_ORDER) - 1  # -1: DONE is end state
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "TurbineUI":
        if self.enabled:
            self._live = Live(
                self._render(),
                console=self._console,
                refresh_per_second=self._refresh,
                screen=False,
            )
            self._live.__enter__()
            self._active = True
        return self

    def __exit__(self, *args: Any) -> None:
        if self._live:
            self._refresh_display()
            self._live.__exit__(*args)
            self._active = False

    # ------------------------------------------------------------------
    # Pipeline event hooks  (called by Manager)
    # ------------------------------------------------------------------

    def on_step(self, step: PipelineStep, message: str = "") -> None:
        """Advance the pipeline to *step*."""
        if not self.enabled:
            return
        self._current_step = step
        if message:
            self._step_messages[step] = message
        completed = _STEP_ORDER.index(step)
        self._progress.update(self._step_task, completed=completed)
        self._refresh_display()

    def on_worker_start(self, ticket_id: str, description: str) -> None:
        if not self.enabled:
            return
        state = self._workers.setdefault(
            ticket_id,
            WorkerState(ticket_id=ticket_id, description=description),
        )
        state.start()
        self._refresh_display()

    def on_worker_attempt(self, ticket_id: str, attempt: int) -> None:
        if not self.enabled:
            return
        if ticket_id in self._workers:
            self._workers[ticket_id].attempt = attempt
            self._refresh_display()

    def on_worker_conflict(self, ticket_id: str, detail: str = "") -> None:
        if not self.enabled:
            return
        if ticket_id in self._workers:
            w = self._workers[ticket_id]
            w.status = WorkerStatus.CONFLICT
            w.detail = detail
            self._refresh_display()

    def on_worker_done(
        self,
        ticket_id: str,
        success: bool,
        detail: str = "",
        files: list[str] | None = None,  # Phase 18: ignored by TurbineUI (rich dashboard)
    ) -> None:
        if not self.enabled:
            return
        if ticket_id in self._workers:
            w = self._workers[ticket_id]
            w.finish(success)
            w.detail = detail
            self._refresh_display()

    def on_worker_repair(
        self,
        ticket_id: str,
        files: list[str] | None = None,  # Phase 18: ignored by TurbineUI
    ) -> None:
        if not self.enabled:
            return
        if ticket_id in self._workers:
            self._workers[ticket_id].status = WorkerStatus.REPAIR
            self._refresh_display()

    def on_worker_token(self, ticket_id: str, char_count: int) -> None:
        """Phase 16: update the live token/char counter for a running worker.

        Called rapidly as streaming token chunks arrive; only updates the
        in-memory counter and redraws — no status change.
        """
        if not self.enabled:
            return
        if ticket_id in self._workers:
            self._workers[ticket_id].token_chars = char_count
            self._refresh_display()

    def on_mode(self, mode: Any) -> None:
        """Phase 20: notify the UI which pipeline mode was selected (WIDE/DEEP)."""
        if not self.enabled:
            return
        # mode is a PipelineMode enum — show it in the header message
        label = mode.value.upper() if hasattr(mode, "value") else str(mode)
        self._step_messages[self._current_step] = (
            self._step_messages.get(self._current_step, "") + f"  [{label} mode]"
        ).strip()
        self._refresh_display()

    def on_deep_iteration(self, ticket_id: str, iteration: int, last_tool: str = "") -> None:
        """Phase 20: update the worker row with the current Deep Mode iteration."""
        if not self.enabled:
            return
        if ticket_id in self._workers:
            w = self._workers[ticket_id]
            w.attempt = iteration
            if last_tool:
                w.detail = f"tool: {last_tool}"
            self._refresh_display()

    def on_done(self, summary: str = "") -> None:
        if not self.enabled:
            return
        self._current_step = PipelineStep.DONE
        self._step_messages[PipelineStep.DONE] = summary
        self._progress.update(self._step_task, completed=len(_STEP_ORDER) - 1)
        self._refresh_display()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _refresh_display(self) -> None:
        if self._live and self._active:
            self._live.update(self._render())

    def _render(self) -> Table:
        root = Table.grid(padding=(0, 1))
        root.add_column()

        # Header
        step_label = self._current_step.value
        msg = self._step_messages.get(self._current_step, "")
        header_text = Text()
        header_text.append("⚙ Turbine", style="bold white")
        if self.title:
            header_text.append(f"  {self.title}", style="dim")
        header_text.append(f"  ▶  {step_label}", style="bold cyan")
        if msg:
            header_text.append(f"  —  {msg}", style="dim")
        root.add_row(Panel(header_text, style="bright_blue"))

        # Progress bar
        root.add_row(self._progress)

        # Worker table (only when there are workers)
        if self._workers:
            root.add_row(self._build_worker_table())

        return root

    def _build_worker_table(self) -> Panel:
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 1),
            expand=True,
        )
        table.add_column("Worker", style="white", min_width=10)
        table.add_column("Status",  min_width=12)
        table.add_column("Attempt", justify="right", min_width=7)
        table.add_column("Elapsed", justify="right", min_width=8)
        table.add_column("Description")

        for w in self._workers.values():
            w.tick_elapsed()
            style = _STATUS_STYLE[w.status]
            icon  = _STATUS_ICON[w.status]
            status_cell = Text(f"{icon} {w.status.value}", style=style)
            attempt_cell = str(w.attempt) if w.attempt else "—"
            elapsed_cell = f"{w.elapsed:.1f}s" if w.elapsed else "—"
            desc = w.description[:60] + "…" if len(w.description) > 60 else w.description
            # Phase 16: show live char count while the worker is actively streaming
            if w.detail:
                desc += f"  [dim]{w.detail[:40]}[/dim]"
            elif w.status == WorkerStatus.RUNNING and w.token_chars:
                desc += f"  [dim]{w.token_chars:,} chars…[/dim]"
            table.add_row(
                w.ticket_id,
                status_cell,
                attempt_cell,
                elapsed_cell,
                desc,
            )

        return Panel(table, title="Workers", border_style="bright_black")
