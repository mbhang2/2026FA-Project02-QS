"""Tests for the metrics bundle (Tasks 3.1–3.6)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.metrics.composite import composite_score
from src.metrics.concentration import pnl_concentration, turnover
from src.metrics.decay import MAX_DECAY_RATIO, decay_check
from src.metrics.returns import (
    annualized_volatility,
    cumulative_return,
    sharpe_ratio,
)
from src.metrics.risk import cvar, max_drawdown
from src.metrics.stability import MAX_STABILITY, subperiod_stability


# --- Task 3.1: returns ---


def test_cumulative_return_closed_form():
    equity = pd.Series([1.0, 1.1, 1.21])
    assert cumulative_return(equity) == pytest.approx(0.21)


def test_sharpe_ratio_zero_mean_closed_form():
    r = pd.Series([0.01, -0.01, 0.01, -0.01])
    assert sharpe_ratio(r) == pytest.approx(0.0, abs=1e-9)


def test_annualized_volatility_known():
    r = pd.Series([0.01, -0.01, 0.0, 0.02])
    std = np.std(np.asarray([0.01, -0.01, 0.0, 0.02]), ddof=1)
    assert annualized_volatility(r) == pytest.approx(std * np.sqrt(252))


# --- Task 3.2: risk ---


def test_max_drawdown_increasing_is_zero():
    equity = pd.Series([1.0, 1.1, 1.2, 1.3])
    assert max_drawdown(equity) == 0.0


def test_max_drawdown_known_drop():
    equity = pd.Series([1.0, 1.2, 0.6, 1.3])
    # peak 1.2 -> trough 0.6 => drawdown (1.2-0.6)/1.2 = 0.5
    assert max_drawdown(equity) == pytest.approx(0.5)


def test_cvar_known():
    r = pd.Series([-0.10, -0.05, 0.0, 0.01, 0.02])
    # alpha=0.2, n=5 -> k=ceil(1)=1 worst return = -0.10 -> cvar = 0.10
    assert cvar(r, alpha=0.2) == pytest.approx(0.10)


# --- Task 3.3: stability ---


def test_stability_identical_windows_maximum():
    w = [0.02, 0.015, 0.01, 0.025, 0.02]
    r = pd.Series(w * 4)
    assert subperiod_stability(r, n_windows=4)["stability_score"] == pytest.approx(
        0.0, abs=1e-9
    )


def test_stability_one_strong_window_lower():
    strong = [0.02, 0.015, 0.01, 0.025, 0.02]
    weak = [0.01, -0.01, 0.01, -0.01, 0.01]
    balanced = subperiod_stability(pd.Series(strong * 4), n_windows=4)[
        "stability_score"
    ]
    imbalanced = subperiod_stability(pd.Series(strong + weak * 3), n_windows=4)[
        "stability_score"
    ]
    assert imbalanced < balanced


def test_stability_never_non_finite():
    import math

    patterns = [
        [0.05] * 5 + [-0.05] * 5 + [0.05] * 5 + [-0.05] * 5,
        [0.02] * 6 + [-0.10] * 2 + [0.00] * 8 + [0.03] * 4,
        [0.10] * 4 + [0.10, -0.90] * 8,
    ]
    for p in patterns:
        score = subperiod_stability(pd.Series(p), n_windows=4)["stability_score"]
        assert math.isfinite(score)
        assert -MAX_STABILITY - 1e-9 <= score <= 1e-9


# --- Task 3.4: concentration ---


def test_turnover_constant_low():
    pos = pd.Series([1.0] * 100)
    # entry trade only: 1.0 / 100 bars
    assert turnover(pos) == pytest.approx(1.0 / 100)


def test_pnl_concentration_dominant_day_high():
    r = pd.Series([0.001, 0.002, 0.5, 0.001])
    assert pnl_concentration(r) > 0.9


def test_pnl_concentration_even_low():
    r = pd.Series([0.01, 0.01, 0.01, 0.01])
    assert pnl_concentration(r) == pytest.approx(0.25)


# --- Task 3.5: decay ---


def test_decay_no_decay_when_equal():
    val = {"sharpe_ratio": 1.0, "cumulative_return": 0.5}
    hold = {"sharpe_ratio": 1.0, "cumulative_return": 0.5}
    result = decay_check(val, hold)
    assert result["decay_flag"] is False
    assert result["sharpe_decay_ratio"] == pytest.approx(1.0)


def test_decay_flag_when_holdout_collapses():
    val = {"sharpe_ratio": 2.0, "cumulative_return": 1.0}
    hold = {"sharpe_ratio": 0.1, "cumulative_return": 0.05}
    result = decay_check(val, hold)
    assert result["decay_flag"] is True


def test_decay_ratio_never_non_finite():
    import math

    # Validation Sharpe of 0.0 (no edge) with non-zero holdout previously
    # produced inf and broke JSON serialization.
    r = decay_check(
        {"sharpe_ratio": 0.0, "cumulative_return": 0.0},
        {"sharpe_ratio": 0.4, "cumulative_return": 0.2},
    )
    assert math.isfinite(r["sharpe_decay_ratio"])
    assert math.isfinite(r["cr_decay_ratio"])
    assert abs(r["sharpe_decay_ratio"]) <= MAX_DECAY_RATIO
    assert abs(r["cr_decay_ratio"]) <= MAX_DECAY_RATIO


# --- Task 3.6: composite ---


def test_composite_adds_score_without_mutating_input():
    metrics = {
        "sharpe_ratio": 1.5,
        "max_drawdown": 0.2,
        "stability_score": -0.1,
        "decay_flag": False,
    }
    before = dict(metrics)
    score = composite_score(metrics)
    assert metrics == before
    assert score == pytest.approx(1.5 - 0.2 + (-0.1))


def test_composite_weights_change_score():
    metrics = {
        "sharpe_ratio": 1.0,
        "max_drawdown": 0.0,
        "stability_score": 0.0,
        "decay_flag": False,
    }
    assert composite_score(metrics, {"sharpe": 2.0}) == pytest.approx(2.0)


def test_composite_decay_penalty():
    metrics = {
        "sharpe_ratio": 1.0,
        "max_drawdown": 0.0,
        "stability_score": 0.0,
        "decay_flag": True,
    }
    assert composite_score(metrics) == pytest.approx(0.0)
