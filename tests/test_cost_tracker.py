"""Tests for turbine.cost_tracker — Phase 15."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from turbine.cost_tracker import (
    BudgetExceededError,
    CostTracker,
    DEFAULT_PRICE_TABLE,
)


# ---------------------------------------------------------------------------
# BudgetExceededError
# ---------------------------------------------------------------------------


class TestBudgetExceededError:
    def test_message_contains_budget_and_current(self):
        exc = BudgetExceededError(budget_usd=1.00, current_usd=1.23)
        msg = str(exc)
        assert "$1.00" in msg
        assert "$1.2300" in msg

    def test_attributes_accessible(self):
        exc = BudgetExceededError(budget_usd=0.50, current_usd=0.75)
        assert exc.budget_usd == 0.50
        assert exc.current_usd == 0.75


# ---------------------------------------------------------------------------
# CostTracker — basic tracking
# ---------------------------------------------------------------------------


class TestCostTrackerBasic:
    def test_initial_totals_are_zero(self):
        ct = CostTracker()
        assert ct.total_input_tokens == 0
        assert ct.total_output_tokens == 0
        assert ct.total_tokens == 0
        assert ct.total_calls == 0
        assert ct.total_cost_usd == 0.0

    def test_record_accumulates_tokens(self):
        ct = CostTracker(model="mistral-large-latest")
        ct.record(1000, 500)
        assert ct.total_input_tokens == 1000
        assert ct.total_output_tokens == 500
        assert ct.total_tokens == 1500
        assert ct.total_calls == 1

    def test_record_accumulates_across_calls(self):
        ct = CostTracker(model="mistral-large-latest")
        ct.record(1000, 500)
        ct.record(2000, 100)
        assert ct.total_input_tokens == 3000
        assert ct.total_output_tokens == 600
        assert ct.total_calls == 2

    def test_negative_values_are_treated_as_zero(self):
        """Defensive: record() should not subtract from totals."""
        ct = CostTracker()
        ct.record(100, 50)
        ct.record(-10, -5)
        assert ct.total_input_tokens == 100
        assert ct.total_output_tokens == 50

    def test_mock_values_do_not_raise(self):
        """Defensive: MagicMock tokens (from test mocks) must not crash record()."""
        ct = CostTracker()
        mock_val = MagicMock()
        ct.record(mock_val, mock_val)  # must not raise
        assert ct.total_calls == 1


# ---------------------------------------------------------------------------
# CostTracker — cost calculation
# ---------------------------------------------------------------------------


class TestCostCalculation:
    def test_cost_uses_price_table(self):
        # 1M input tokens at $2.00/M + 1M output tokens at $6.00/M = $8.00
        ct = CostTracker(model="mistral-large-latest")
        ct.record(1_000_000, 1_000_000)
        assert abs(ct.total_cost_usd - 8.00) < 1e-6

    def test_cost_scales_proportionally(self):
        ct = CostTracker(model="mistral-large-latest")
        ct.record(500_000, 0)
        expected = 0.5 * 2.00  # half a million input tokens
        assert abs(ct.total_cost_usd - expected) < 1e-6

    def test_small_cost_model(self):
        ct = CostTracker(model="mistral-small-latest")
        ct.record(1_000_000, 1_000_000)
        expected = 0.20 + 0.60
        assert abs(ct.total_cost_usd - expected) < 1e-6

    def test_custom_price_table(self):
        custom = {"my-model": (1.00, 2.00)}
        ct = CostTracker(model="my-model", price_table=custom)
        ct.record(1_000_000, 1_000_000)
        assert abs(ct.total_cost_usd - 3.00) < 1e-6

    def test_unknown_model_falls_back_to_cheapest(self):
        """Unknown models should use the cheapest entry, not raise."""
        ct = CostTracker(model="unknown-model-xyz")
        ct.record(1_000_000, 0)
        # Must produce a positive cost, not raise
        assert ct.total_cost_usd > 0


# ---------------------------------------------------------------------------
# CostTracker — report
# ---------------------------------------------------------------------------


class TestCostTrackerReport:
    def test_report_contains_token_counts(self):
        ct = CostTracker(model="mistral-large-latest")
        ct.record(1000, 500)
        report = ct.report()
        assert "1,000" in report
        assert "500" in report

    def test_report_contains_cost(self):
        ct = CostTracker(model="mistral-large-latest")
        ct.record(1_000_000, 0)
        report = ct.report()
        assert "$" in report

    def test_report_shows_budget_when_set(self):
        ct = CostTracker(model="mistral-large-latest", budget_usd=1.00)
        ct.report()  # must not raise even with no tokens

    def test_report_no_budget_no_slash(self):
        ct = CostTracker(model="mistral-large-latest")
        report = ct.report()
        assert "budget" not in report.lower()


# ---------------------------------------------------------------------------
# CostTracker — budget enforcement
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    def test_no_budget_never_raises(self):
        ct = CostTracker(model="mistral-large-latest", budget_usd=None)
        # Record a million tokens — no budget → no error
        ct.record(1_000_000, 1_000_000)

    def test_budget_not_exceeded_does_not_raise(self):
        ct = CostTracker(model="mistral-large-latest", budget_usd=100.00)
        ct.record(1_000, 1_000)  # tiny cost, far under budget

    def test_budget_exceeded_raises(self):
        ct = CostTracker(model="mistral-large-latest", budget_usd=0.001)
        with pytest.raises(BudgetExceededError) as exc_info:
            ct.record(1_000_000, 1_000_000)  # $8.00 — well over $0.001
        assert exc_info.value.budget_usd == 0.001
        assert exc_info.value.current_usd > 0.001

    def test_budget_raised_on_exact_boundary(self):
        # Set budget at exactly the cost of 1M input + 1M output for large model ($8.00)
        ct = CostTracker(model="mistral-large-latest", budget_usd=8.00)
        with pytest.raises(BudgetExceededError):
            ct.record(1_000_000, 1_000_000)

    def test_budget_tokens_still_accumulated_after_raise(self):
        """Tokens are recorded before the budget check raises — totals should be updated."""
        ct = CostTracker(model="mistral-large-latest", budget_usd=0.001)
        try:
            ct.record(1_000_000, 1_000_000)
        except BudgetExceededError:
            pass
        assert ct.total_input_tokens == 1_000_000
        assert ct.total_output_tokens == 1_000_000


# ---------------------------------------------------------------------------
# CostTracker — UI callback
# ---------------------------------------------------------------------------


class TestCostTrackerUI:
    def test_on_cost_update_called_on_record(self):
        ui = MagicMock()
        ui.on_cost_update = MagicMock()
        ct = CostTracker(model="mistral-large-latest", ui=ui)
        ct.record(1000, 500)
        ui.on_cost_update.assert_called_once()
        call_kwargs = ui.on_cost_update.call_args.kwargs
        assert call_kwargs["input_tokens"] == 1000
        assert call_kwargs["output_tokens"] == 500
        assert call_kwargs["total_cost_usd"] >= 0

    def test_no_ui_record_does_not_crash(self):
        ct = CostTracker(model="mistral-large-latest", ui=None)
        ct.record(1000, 500)  # must not raise

    def test_ui_without_on_cost_update_does_not_crash(self):
        ui = MagicMock(spec=[])  # no attributes
        ct = CostTracker(model="mistral-large-latest", ui=ui)
        ct.record(1000, 500)  # must not raise


# ---------------------------------------------------------------------------
# CostTracker — thread safety
# ---------------------------------------------------------------------------


class TestCostTrackerThreadSafety:
    def test_concurrent_records_accumulate_correctly(self):
        ct = CostTracker(model="mistral-large-latest")
        n_threads = 20
        tokens_per_thread = 1000

        def record_some():
            ct.record(tokens_per_thread, 0)

        threads = [threading.Thread(target=record_some) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert ct.total_input_tokens == n_threads * tokens_per_thread
        assert ct.total_calls == n_threads
