"""CostTracker — Phase 15.

Accumulates input and output token counts for every LLM call made during a
Turbine run, computes a USD cost estimate, enforces an optional budget ceiling,
and emits structured cost events via :class:`~turbine.json_ui.JsonEventUI`.

Price table
-----------
Prices are in USD per 1 000 000 tokens (published Mistral list prices as of
April 2026).  Pass a custom ``price_table`` to :class:`CostTracker` if your
contract has different rates or if you use a model not listed below.

Usage
-----
A single :class:`CostTracker` is created by :class:`~turbine.manager.Manager`
and passed into every :class:`~turbine.worker.Worker`.  After each API call
both parties call :meth:`CostTracker.record`; the tracker atomically
accumulates the totals, optionally emits a ``cost_update`` JSON event, and
raises :class:`BudgetExceededError` when the cumulative estimate crosses
the configured ceiling.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Default price table
# ---------------------------------------------------------------------------

#: ``{model_name: (input_usd_per_1m, output_usd_per_1m)}``
DEFAULT_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "mistral-large-latest":  (2.00,  6.00),
    "mistral-small-latest":  (0.20,  0.60),
    "codestral-latest":      (0.20,  0.60),
    "open-mistral-7b":       (0.25,  0.25),
    "open-mixtral-8x7b":     (0.70,  0.70),
    "open-mixtral-8x22b":    (2.00,  6.00),
}


# ---------------------------------------------------------------------------
# BudgetExceededError
# ---------------------------------------------------------------------------


class BudgetExceededError(Exception):
    """Raised when the accumulated LLM cost would exceed the configured budget."""

    def __init__(self, budget_usd: float, current_usd: float) -> None:
        self.budget_usd = budget_usd
        self.current_usd = current_usd
        super().__init__(
            f"Budget of ${budget_usd:.2f} exceeded "
            f"(current estimate: ${current_usd:.4f}). "
            "Aborting — no further LLM calls will be made."
        )


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------


class CostTracker:
    """Thread-safe accumulator of LLM token counts and cost estimates.

    Parameters
    ----------
    model:
        Default model name used when :meth:`record` is called without an
        explicit *model* argument.
    budget_usd:
        Optional spend ceiling in USD.  :meth:`record` raises
        :class:`BudgetExceededError` when the cumulative cost reaches or
        exceeds this value.  ``None`` disables the budget check.
    price_table:
        ``{model_name: (input_usd_per_1m, output_usd_per_1m)}`` mapping.
        Defaults to :data:`DEFAULT_PRICE_TABLE`.
    ui:
        Optional :class:`~turbine.json_ui.JsonEventUI` instance.  When set,
        every :meth:`record` call emits a ``cost_update`` event.
    """

    def __init__(
        self,
        model: str = "mistral-large-latest",
        budget_usd: float | None = None,
        price_table: dict[str, tuple[float, float]] | None = None,
        ui: Any = None,
    ) -> None:
        self._model = model
        self._budget_usd = budget_usd
        self._price_table = price_table if price_table is not None else DEFAULT_PRICE_TABLE
        self._ui = ui
        self._lock = threading.Lock()

        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_calls: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def record(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str | None = None,
    ) -> None:
        """Record token counts for one LLM call.

        Atomically updates running totals, emits a ``cost_update`` JSON event
        if a UI is configured, then checks the budget.

        Parameters
        ----------
        input_tokens:
            Prompt tokens consumed by this call.
        output_tokens:
            Completion tokens produced by this call.
        model:
            Model used for this call (defaults to the tracker's default model).

        Raises
        ------
        BudgetExceededError
            If the cumulative cost now meets or exceeds the configured budget.
        """
        with self._lock:
            try:
                inp = max(0, int(input_tokens)) if input_tokens else 0
                out = max(0, int(output_tokens)) if output_tokens else 0
            except (TypeError, ValueError):
                inp, out = 0, 0
            self._total_input_tokens += inp
            self._total_output_tokens += out
            self._total_calls += 1
            current_cost = self._compute_cost(
                self._total_input_tokens,
                self._total_output_tokens,
                model or self._model,
            )

        # Emit cost_update outside the lock to avoid holding it during I/O
        if self._ui is not None and hasattr(self._ui, "on_cost_update"):
            self._ui.on_cost_update(
                input_tokens=self._total_input_tokens,
                output_tokens=self._total_output_tokens,
                total_cost_usd=current_cost,
            )

        if self._budget_usd is not None and current_cost >= self._budget_usd:
            raise BudgetExceededError(
                budget_usd=self._budget_usd,
                current_usd=current_cost,
            )

    @property
    def total_input_tokens(self) -> int:
        with self._lock:
            return self._total_input_tokens

    @property
    def total_output_tokens(self) -> int:
        with self._lock:
            return self._total_output_tokens

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return self._total_input_tokens + self._total_output_tokens

    @property
    def total_calls(self) -> int:
        with self._lock:
            return self._total_calls

    @property
    def total_cost_usd(self) -> float:
        with self._lock:
            return self._compute_cost(
                self._total_input_tokens,
                self._total_output_tokens,
                self._model,
            )

    def report(self) -> str:
        """Return a one-line human-readable cost summary."""
        with self._lock:
            inp = self._total_input_tokens
            out = self._total_output_tokens
            calls = self._total_calls
            cost = self._compute_cost(inp, out, self._model)
        budget_str = (
            f" / budget ${self._budget_usd:.2f}" if self._budget_usd is not None else ""
        )
        return (
            f"Tokens: {inp:,} in + {out:,} out = {inp+out:,} total "
            f"over {calls} call(s) — "
            f"estimated cost: ${cost:.4f}{budget_str}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_cost(
        self, input_tokens: int, output_tokens: int, model: str
    ) -> float:
        """Return USD cost for the given token counts and model."""
        # Walk through the price table looking for the best match.
        # Exact match first; then prefer the longest prefix match so that
        # versioned model names (e.g. "mistral-large-2411") fall back to
        # their base entry ("mistral-large-latest") gracefully.
        prices = self._price_table.get(model)
        if prices is None:
            # Longest-prefix fallback
            for key in sorted(self._price_table, key=len, reverse=True):
                if model.startswith(key.rstrip("-latest").rstrip("-")):
                    prices = self._price_table[key]
                    break
        if prices is None:
            # Unknown model — use the cheapest known rate as a conservative floor
            prices = min(self._price_table.values(), key=lambda p: p[0])

        input_price, output_price = prices
        return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
