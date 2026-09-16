"""Equity curve / PnL accounting (Task 2.3)."""

from __future__ import annotations

import pandas as pd

from alternatives.l1_engine.execution.simulator import simulate_fill


def build_equity_curve(
    positions: pd.Series,
    prices: pd.DataFrame,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    """Walk ``positions`` bar by bar and accumulate an equity curve.

    ``positions[i]`` is the target position held over bar *i* (set at bar *i*'s
    open, liquidated at its close) and is index-aligned with ``prices``. The
    lag that makes this leakage-safe — deciding at bar *t*'s close to act at bar
    *t+1*'s open — is produced by the orchestrator via a one-bar shift before
    calling this function.

    Returns a frame with columns ``equity``, ``daily_return``, ``position``,
    ``transaction_cost``, indexed by bar timestamp.
    """
    equity = 1.0
    prev_position = 0.0
    records = []

    for ts, new_position in positions.items():
        bar = prices.loc[ts]
        fill = simulate_fill(prev_position, new_position, bar, fee_bps, slippage_bps)
        traded_cost = fill["transaction_cost"] + fill["slippage_cost"]

        open_p = float(bar["open"])
        close_p = float(bar["close"])
        if open_p == 0.0:
            price_return = 0.0
        else:
            price_return = new_position * (close_p / open_p - 1.0)

        daily_return = price_return - traded_cost
        equity *= 1.0 + daily_return

        records.append(
            {
                "equity": equity,
                "daily_return": daily_return,
                "position": float(new_position),
                "transaction_cost": traded_cost,
            }
        )
        prev_position = new_position

    return pd.DataFrame(records, index=positions.index)
