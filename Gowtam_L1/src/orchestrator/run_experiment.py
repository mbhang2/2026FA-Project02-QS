"""The fixed L1 entrypoint — ``run_experiment`` (Task 4.1) and its CLI (Task 4.3).

This is the only file that changes when L1 itself gains features; it is never
touched to accommodate a specific strategy.
"""

from __future__ import annotations

import pandas as pd

from src.config.experiment_spec import ExperimentSpec
from src.data.loader import load_prices_pit
from src.data.ticks import load_bars
from src.execution.leakage_guard import assert_causal_vectorized, assert_no_lookahead
from src.execution.portfolio import build_equity_curve
from src.metrics.composite import composite_score
from src.metrics.concentration import pnl_concentration, turnover
from src.metrics.decay import decay_check
from src.metrics.returns import (
    annualized_volatility,
    cumulative_return,
    sharpe_ratio,
)
from src.metrics.risk import cvar, max_drawdown
from src.metrics.stability import subperiod_stability
from src.splitting.windows import DateRange, split_periods
from src.strategy.loader import load_strategy
from src.strategy.protocol import VectorizedStrategy

# Adapter boundary: where price data comes from. ``"placeholder.csv"`` remains
# the dummy CSV default; ``TICK_SOURCE`` selects the raw tick corpus in
# ``Data/raw/Ticks/`` and resamples it to ``spec.data_frequency`` on-demand.
DEFAULT_DATA_SOURCE = "placeholder.csv"
TICK_SOURCE = "ticks"


def _load_prices(spec: ExperimentSpec) -> pd.DataFrame:
    """Load the single-instrument bar frame for ``spec`` (canonical schema).

    Returns a frame indexed by ``timestamp`` with only OHLCV columns (no
    ``asset_id``), for ``spec.asset_ids[0]``.
    """
    asset = spec.asset_ids[0]

    if spec.data_source == TICK_SOURCE:
        bars = load_bars(asset, spec.start, spec.end, freq=spec.data_frequency)
        return bars.drop(columns=["asset_id"])

    prices = load_prices_pit(
        spec.data_source,
        spec.asset_ids,
        as_of=pd.Timestamp(spec.end),
        start=spec.start,
        end=spec.end,
    )
    return prices[prices["asset_id"] == asset].drop(columns=["asset_id"])


def run_experiment(spec: ExperimentSpec) -> dict:
    """Run one strategy end-to-end and return a structured, serializable result.

    Steps: validate spec -> load strategy -> load data (CSV or tick-resampled
    bars) -> split train/validation/holdout -> simulate the strategy over the
    validation and holdout windows (walking bar by bar under the no-lookahead
    guard) -> compute the full metrics bundle for both windows -> decay check ->
    composite score.
    """
    spec.validate()
    strategy = load_strategy(spec.strategy_module_path)

    prices = _load_prices(spec)

    # Vectorized fast path: if the strategy offers a causal whole-series
    # implementation, verify it does not look ahead once up front (O(n)), then
    # use it instead of the per-bar loop.
    if isinstance(strategy, VectorizedStrategy):
        assert_causal_vectorized(strategy, prices)

    # L1's execution model is single-instrument: one price path -> one position
    # series -> one equity curve. Simulate the first requested asset; multi-asset
    # portfolio construction is out of scope for this engine.
    train, validation, holdout = split_periods(spec)

    val_run = _run_window(strategy, prices, validation, spec)
    hold_run = _run_window(strategy, prices, holdout, spec)

    val_metrics = _compute_metrics(val_run["curve"], val_run["positions"])
    hold_metrics = _compute_metrics(hold_run["curve"], hold_run["positions"])

    decay = decay_check(val_metrics, hold_metrics)

    composite = composite_score({**val_metrics, **decay})

    return {
        "validation_metrics": val_metrics,
        "holdout_metrics": hold_metrics,
        "decay": decay,
        "composite_score": composite,
    }


def _run_window(
    strategy, prices: pd.DataFrame, rng: DateRange, spec: ExperimentSpec
) -> dict:
    """Simulate ``strategy`` over ``rng``, deciding each bar from prior history.

    Vectorized strategies are evaluated in one causal pass; otherwise the
    per-bar path runs with a monotonic cursor (O(n) orchestration, matching a
    one-bar shift so no future bar is visible).
    """
    position_series = _positions_for_window(strategy, prices, rng)
    curve = build_equity_curve(
        position_series, prices.loc[position_series.index], spec.fee_bps, spec.slippage_bps
    )
    return {"curve": curve, "positions": position_series}


def _positions_for_window(
    strategy, prices: pd.DataFrame, rng: DateRange
) -> pd.Series:
    """Return the position series over ``rng`` (point-in-time safe)."""
    window_mask = (prices.index >= rng.start) & (prices.index <= rng.end)

    if isinstance(strategy, VectorizedStrategy):
        positions = strategy.generate_positions_vectorized(prices)
        return positions[window_mask].astype(float).sort_index()

    timeline = prices.index
    window_ts = timeline[window_mask]

    positions: dict = {}
    n = len(timeline)
    k = 0  # number of bars strictly before the current bar (monotonic, non-decreasing)
    for ts in window_ts:
        while k < n and timeline[k] < ts:
            k += 1
        if k == 0:
            position = 0.0
        else:
            history = prices.iloc[:k]  # O(1) slice; no O(n) mask per bar
            assert_no_lookahead(history, ts)
            position = float(strategy.generate_positions(history).iloc[-1])
        positions[ts] = position

    return pd.Series(positions, dtype=float).sort_index()


def _compute_metrics(curve: pd.DataFrame, positions: pd.Series) -> dict:
    daily_returns = curve["daily_return"]
    stability = subperiod_stability(daily_returns)
    return {
        "cumulative_return": float(cumulative_return(curve["equity"])),
        "sharpe_ratio": float(sharpe_ratio(daily_returns)),
        "annualized_volatility": float(annualized_volatility(daily_returns)),
        "max_drawdown": float(max_drawdown(curve["equity"])),
        "cvar": float(cvar(daily_returns)),
        "stability_score": float(stability["stability_score"]),
        "per_window_sharpe": [float(x) for x in stability["per_window_sharpe"]],
        "turnover": float(turnover(positions)),
        "pnl_concentration": float(pnl_concentration(daily_returns)),
    }


if __name__ == "__main__":
    import argparse
    import json

    from src.serialization.trial_log import write_trial_log

    parser = argparse.ArgumentParser(description="Run an L1 experiment from a spec file.")
    parser.add_argument("--spec", required=True, help="path to spec JSON")
    parser.add_argument("--output", default=None, help="trial log output path")
    args = parser.parse_args()

    with open(args.spec, "r") as f:
        spec = ExperimentSpec.from_dict(json.load(f))

    print(f"[run_experiment] validated spec: {spec.asset_ids} [{spec.start}..{spec.end}]")
    print("[run_experiment] loading strategy:", spec.strategy_module_path)
    result = run_experiment(spec)
    print("[run_experiment] finished; composite score =", result["composite_score"])

    output = args.output or f"{spec.output_dir}/trial_log.json"
    path = write_trial_log(result, spec, output)
    print("[run_experiment] trial log written to", path)
