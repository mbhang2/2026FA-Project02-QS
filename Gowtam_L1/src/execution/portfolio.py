"""Equity curve / PnL accounting (Task 2.3)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def build_equity_curve(
    positions: pd.Series,
    prices: pd.DataFrame,
    fee_bps: float,
    slippage_bps: float,
) -> pd.DataFrame:
    """Accumulate an equity curve from a position series (vectorized).

    ``positions[i]`` is the target position held over bar *i* (set at bar *i*'s
    open, liquidated at its close) and is index-aligned with ``prices``. The lag
    that makes this leakage-safe — deciding at bar *t*'s close to act at bar
    *t+1*'s open — is produced by the orchestrator via a one-bar shift before
    calling this function.

    Costs are charged on the traded notional ``abs(new - prev)``, matching
    ``simulate_fill`` (``transaction_cost + slippage_cost`` combined). Returns a
    frame with columns ``equity``, ``daily_return``, ``position``,
    ``transaction_cost``, indexed by bar timestamp.
    """
    pos = np.asarray(positions.to_numpy(dtype=float))
    if pos.size == 0:
        return pd.DataFrame(
            columns=["equity", "daily_return", "position", "transaction_cost"],
            index=positions.index,
        )

    open_p = np.asarray(prices["open"].to_numpy(dtype=float))
    close_p = np.asarray(prices["close"].to_numpy(dtype=float))

    prev = np.empty_like(pos)
    prev[0] = 0.0
    if pos.size > 1:
        prev[1:] = pos[:-1]

    traded = np.abs(pos - prev)
    cost = traded * (fee_bps + slippage_bps) / 10000.0

    nonzero_open = open_p != 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        price_return = np.where(
            nonzero_open, pos * (close_p / open_p - 1.0), 0.0
        )

    daily_return = price_return - cost
    equity = np.cumprod(1.0 + daily_return)

    return pd.DataFrame(
        {
            "equity": equity,
            "daily_return": daily_return,
            "position": pos,
            "transaction_cost": cost,
        },
        index=positions.index,
    )
