"""Tests for the fill simulator and portfolio accounting (Tasks 2.2 and 2.3)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.execution.portfolio import build_equity_curve
from src.execution.simulator import simulate_fill


def _prices(opens, closes):
    idx = pd.date_range("2024-01-01", periods=len(opens), tz="UTC")
    return pd.DataFrame({"open": opens, "close": closes}, index=idx)


# --- Task 2.2: fill simulator ---


def test_zero_change_zero_cost():
    bar = pd.Series({"open": 100.0})
    fill = simulate_fill(1.0, 1.0, bar, fee_bps=10.0, slippage_bps=5.0)
    assert fill["transaction_cost"] == 0.0
    assert fill["slippage_cost"] == 0.0


def test_full_flip_costs_double_half():
    bar = pd.Series({"open": 100.0})
    full = simulate_fill(-1.0, 1.0, bar, fee_bps=10.0, slippage_bps=0.0)
    half = simulate_fill(0.0, 1.0, bar, fee_bps=10.0, slippage_bps=0.0)
    assert full["transaction_cost"] == pytest.approx(2 * half["transaction_cost"])


# --- Task 2.3: portfolio ---


def test_equity_curve_hand_computed():
    prices = _prices([100.0, 100.0, 100.0], [100.0, 100.0, 100.0])
    positions = pd.Series([0.0, 1.0, 1.0], index=prices.index)
    curve = build_equity_curve(positions, prices, fee_bps=10.0, slippage_bps=0.0)
    # only trade is 0 -> 1 at bar index 1, cost = 1.0 * 10/10000 = 0.001
    assert curve.loc[prices.index[1], "transaction_cost"] == pytest.approx(0.001)
    assert curve.loc[prices.index[2], "equity"] == pytest.approx(0.999)
    assert list(curve.columns) == [
        "equity",
        "daily_return",
        "position",
        "transaction_cost",
    ]
