"""JsonEventUI — Phase 6 addition.

A drop-in replacement for TurbineUI that emits newline-delimited JSON events
to stdout instead of rendering a Rich dashboard.  The VS Code extension spawns
Turbine with ``--json-events`` and reads these lines to drive its WebView panel.

Event envelope
--------------
Every line is a JSON object with at minimum:
    {"event": "<name>", "ts": <unix_timestamp_float>, ...payload...}

Events
------
step            step, message
worker_start    ticket_id, description
worker_attempt  ticket_id, attempt
worker_conflict ticket_id, detail
worker_done     ticket_id, success, detail
worker_repair   ticket_id
done            summary
log             level, message   (forwarded from TurbineLogger when in JSON mode)
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from turbine.ui import PipelineStep


class JsonEventUI:
    """Emits newline-delimited JSON events; interface-compatible with TurbineUI."""

    def __init__(self) -> None:
        self.enabled = True  # always on — caller chose --json-events

    # ------------------------------------------------------------------
    # Context manager (no-op — no Live panel to manage)
    # ------------------------------------------------------------------

    def __enter__(self) -> "JsonEventUI":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    # ------------------------------------------------------------------
    # Internal emit
    # ------------------------------------------------------------------

    def _emit(self, event: str, **payload: Any) -> None:
        obj = {"event": event, "ts": time.time(), **payload}
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # TurbineUI-compatible hooks
    # ------------------------------------------------------------------

    def on_step(self, step: PipelineStep, message: str = "") -> None:
        self._emit("step", step=step.value, message=message)

    def on_worker_start(self, ticket_id: str, description: str) -> None:
        self._emit("worker_start", ticket_id=ticket_id, description=description)

    def on_worker_attempt(self, ticket_id: str, attempt: int) -> None:
        self._emit("worker_attempt", ticket_id=ticket_id, attempt=attempt)

    def on_worker_conflict(self, ticket_id: str, detail: str = "") -> None:
        self._emit("worker_conflict", ticket_id=ticket_id, detail=detail)

    def on_worker_done(self, ticket_id: str, success: bool, detail: str = "") -> None:
        self._emit("worker_done", ticket_id=ticket_id, success=success, detail=detail)

    def on_worker_repair(self, ticket_id: str) -> None:
        self._emit("worker_repair", ticket_id=ticket_id)

    def on_done(self, summary: str = "") -> None:
        self._emit("done", summary=summary)

    def on_done_detail(
        self,
        *,
        workers_succeeded: int,
        workers_total: int,
        files_written: int,
        diff_lines: int,
        dry_run: bool,
        tickets: list[dict],
        diagnosis: str = "",
    ) -> None:
        """Rich structured summary — emitted after on_done when extra data is available."""
        self._emit(
            "done_detail",
            workers_succeeded=workers_succeeded,
            workers_total=workers_total,
            files_written=files_written,
            diff_lines=diff_lines,
            dry_run=dry_run,
            tickets=tickets,
            diagnosis=diagnosis,
        )

    def log(self, level: str, message: str) -> None:
        """Forward TurbineLogger output as a structured event."""
        self._emit("log", level=level, message=message)
