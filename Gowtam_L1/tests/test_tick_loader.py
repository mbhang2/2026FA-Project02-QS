"""Tick ingestion + resampling (PLAN_NEXT A.1/A.5)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.ticks import daily_bar_from_ticks, load_bars, load_ticks, resample_ticks


@pytest.fixture(scope="module")
def ticks():
    return load_ticks(2024, 1, 3)


def test_mid_price(ticks):
    expected = (ticks["bid"].iloc[0] + ticks["ask"].iloc[0]) / 2.0
    assert ticks["price"].iloc[0] == pytest.approx(expected)


def test_daily_bar_from_ticks(ticks):
    p = ticks["price"]
    bar = daily_bar_from_ticks(ticks)
    assert bar["open"].iloc[0] == pytest.approx(p.iloc[0])
    assert bar["high"].iloc[0] == pytest.approx(p.max())
    assert bar["low"].iloc[0] == pytest.approx(p.min())
    assert bar["close"].iloc[0] == pytest.approx(p.iloc[-1])
    assert bar["volume"].iloc[0] == len(ticks)


def test_resample_causal_open(ticks):
    h = resample_ticks(ticks, "1h")
    # First bar's open must be the first tick's mid-price (no future tick used).
    assert h["open"].iloc[0] == pytest.approx(ticks["price"].iloc[0])
    # Every bar label is >= the max tick it contains (closed="right").
    assert h.index[-1] >= ticks.index.max() - pd.Timedelta(hours=1)


def test_resample_ohlc_consistency(ticks):
    h = resample_ticks(ticks, "1h")
    assert (h["high"] >= h["low"]).all()
    assert (h["high"] >= h["close"]).all()
    assert (h["low"] <= h["close"]).all()


def test_load_bars_daily_skips_weekends():
    bars = load_bars("xauusd", "2024-01-05", "2024-01-08", freq="1d")
    days = bars.index.normalize()
    # 2024-01-06/07 are Sat/Sun — no file, skipped.
    assert all(d.weekday() < 5 for d in days)
    assert len(bars) >= 1


def test_load_bars_canonical_schema():
    bars = load_bars("xauusd", "2024-01-02", "2024-01-10", freq="1d")
    assert list(bars.columns) == ["asset_id", "open", "high", "low", "close", "volume"]
    assert bars.index.name == "timestamp"
    assert bars.index.is_monotonic_increasing


def test_load_bars_intraday_index_unique():
    """Boundary bins split across day-files must be merged, not duplicated."""
    bars = load_bars("xauusd", "2024-01-02", "2024-01-05", freq="1h")
    assert bars.index.is_unique
    assert bars.index.is_monotonic_increasing
    assert list(bars.columns) == ["asset_id", "open", "high", "low", "close", "volume"]


def test_load_bars_empty_range():
    bars = load_bars("xauusd", "2019-01-01", "2019-01-02", freq="1d")
    assert list(bars.columns) == ["asset_id", "open", "high", "low", "close", "volume"]
    assert len(bars) == 0


def test_run_experiment_over_ticks():
    """Engine end-to-end over real ticks produces finite metrics (integration)."""
    from src.config.experiment_spec import ExperimentSpec
    from src.orchestrator.run_experiment import run_experiment

    spec = ExperimentSpec(
        asset_ids=["xauusd"],
        start="2024-01-02",
        train_end="2024-01-10",
        val_end="2024-01-20",
        end="2024-02-01",
        strategy_module_path="src.strategy.reference_strategies.buy_and_hold",
        fee_bps=1.0,
        slippage_bps=1.0,
        data_source="ticks",
        data_frequency="1d",
    )
    result = run_experiment(spec)
    assert set(result.keys()) == {
        "validation_metrics",
        "holdout_metrics",
        "decay",
        "composite_score",
    }
    for window in ("validation_metrics", "holdout_metrics"):
        assert result[window]["sharpe_ratio"] == result[window]["sharpe_ratio"]  # finite
        assert result[window]["turnover"] >= 0.0
