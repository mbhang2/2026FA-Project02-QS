"""The fixed L1 entrypoint — ``run_experiment`` (Task 4.1) and its CLI (Task 4.3).

This is the only file that changes when L1 itself gains features; it is never
touched to accommodate a specific strategy.
"""

from __future__ import annotations

import pandas as pd

from src.config.experiment_spec import ExperimentSpec
from src.data.loader import load_prices_pit
from src.execution.leakage_guard import assert_no_lookahead
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

# Adapter boundary: where price data comes from. Swapped for the real source
# later without touching callers (see ``src/data/loader.py``).
DEFAULT_DATA_SOURCE = "placeholder.csv"


def run_experiment(spec: ExperimentSpec) -> dict:
    """Run one strategy end-to-end and return a structured, serializable result.

    Steps: validate spec -> load strategy -> load point-in-time data -> split
    train/validation/holdout -> simulate the strategy over the validation and
    holdout windows (walking bar by bar under the no-lookahead guard) -> compute
    the full metrics bundle for both windows -> decay check -> composite score.
    """
    spec.validate()
    strategy = load_strategy(spec.strategy_module_path)

    prices = load_prices_pit(
        DEFAULT_DATA_SOURCE,
        spec.asset_ids,
        as_of=pd.Timestamp(spec.end),
        start=spec.start,
        end=spec.end,
    )

    # L1's execution model is single-instrument: one price path -> one position
    # series -> one equity curve. Simulate the first requested asset; multi-asset
    # portfolio construction is out of scope for this engine.
    asset = spec.asset_ids[0]
    prices = prices[prices["asset_id"] == asset].drop(columns=["asset_id"])

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
    """Simulate ``strategy`` over ``rng``, deciding each bar from prior history."""
    timeline = prices.index
    window_ts = timeline[(timeline >= rng.start) & (timeline <= rng.end)]

    positions: dict = {}
    for ts in window_ts:
        # History up to and including the previous bar (decide on close[t-1],
        # execute on open[t]); nothing at-or-after the decided bar is visible.
        history = prices[prices.index < ts]
        assert_no_lookahead(history, ts)
        if len(history) == 0:
            position = 0.0
        else:
            position = float(strategy.generate_positions(history).iloc[-1])
        positions[ts] = position

    position_series = pd.Series(positions, dtype=float).sort_index()
    curve = build_equity_curve(
        position_series, prices.loc[position_series.index], spec.fee_bps, spec.slippage_bps
    )
    return {"curve": curve, "positions": position_series}


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
