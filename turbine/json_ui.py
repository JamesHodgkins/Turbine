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
worker_done     ticket_id, success, detail, files (Phase 18 — list of modified relative paths)
worker_repair   ticket_id, files                  (Phase 18 — files needing repair)
worker_token    ticket_id, char_count             (Phase 16 — streaming progress)
cost_update             input_tokens, output_tokens, total_tokens, total_cost_usd
done                    summary
done_detail             workers_succeeded, workers_total, files_written, diff_lines,
                        dry_run, tickets, diagnosis,
                        total_input_tokens, total_output_tokens, total_cost_usd
clarification_request   question, options    (Phase 19 — pause for user input in --interactive mode)
mode                    mode             (Phase 20 — "wide" or "deep", emitted after routing)
deep_iteration          ticket_id, iteration, last_tool  (Phase 20 — Deep Mode progress)
log                     level, message   (forwarded from TurbineLogger when in JSON mode)
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

    def on_worker_done(
        self,
        ticket_id: str,
        success: bool,
        detail: str = "",
        files: list[str] | None = None,
    ) -> None:
        # Phase 18: include the list of relative file paths the worker modified
        self._emit(
            "worker_done",
            ticket_id=ticket_id,
            success=success,
            detail=detail,
            files=files or [],
        )

    def on_worker_repair(self, ticket_id: str, files: list[str] | None = None) -> None:
        # Phase 18: include files needing repair so the extension can show diagnostics
        self._emit("worker_repair", ticket_id=ticket_id, files=files or [])

    def on_worker_token(self, ticket_id: str, char_count: int) -> None:
        """Phase 16: emitted on every streaming chunk so the WebView can show live progress."""
        self._emit("worker_token", ticket_id=ticket_id, char_count=char_count)

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
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        total_cost_usd: float = 0.0,
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
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_cost_usd=total_cost_usd,
        )

    def on_cost_update(
        self,
        input_tokens: int,
        output_tokens: int,
        total_cost_usd: float,
    ) -> None:
        """Emit a running cost update after every LLM call (Phase 15).

        The VS Code extension listens for these events to display a live
        cost counter without waiting for the run to finish.
        """
        self._emit(
            "cost_update",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            total_cost_usd=total_cost_usd,
        )

    def on_mode(self, mode: Any) -> None:
        """Phase 20: emit the selected pipeline mode (wide/deep)."""
        label = mode.value if hasattr(mode, "value") else str(mode)
        self._emit("mode", mode=label)

    def on_deep_iteration(self, ticket_id: str, iteration: int, last_tool: str = "") -> None:
        """Phase 20: emit a Deep Mode iteration progress event."""
        self._emit(
            "deep_iteration",
            ticket_id=ticket_id,
            iteration=iteration,
            last_tool=last_tool,
        )

    def on_clarification_request(self, question: str, options: list[str]) -> None:
        """Phase 19: emitted when the investigator needs the user to resolve ambiguity.

        The VS Code extension listens for this event and renders an inline
        question widget; the user's answer is sent back via process stdin.
        """
        self._emit("clarification_request", question=question, options=options)

    def log(self, level: str, message: str) -> None:
        """Forward TurbineLogger output as a structured event."""
        self._emit("log", level=level, message=message)
