"""Fill and cost simulation (Task 2.2)."""

from __future__ import annotations

import pandas as pd


def simulate_fill(
    prev_position: float,
    new_position: float,
    bar: pd.Series,
    fee_bps: float,
    slippage_bps: float,
) -> dict:
    """Simulate a fill at ``bar``'s open, returning executed position and costs.

    A position decided on bar *t*'s close executes at bar *t+1*'s open. This lag
    is itself a leakage-prevention choice: the fill price (the open) is never
    known until after the decision is made, so a strategy cannot execute at a
    price it already "saw".

    Position is expressed as a fraction of capital (e.g. -1 .. 1). Costs are
    charged on the traded notional ``abs(new - prev)`` and returned as fractions
    of capital.

    Returns a dict: ``{executed_position, fill_price, transaction_cost,
    slippage_cost}``.
    """
    delta = new_position - prev_position
    traded = abs(delta)

    transaction_cost = traded * fee_bps / 10000.0
    slippage_cost = traded * slippage_bps / 10000.0
    fill_price = float(bar["open"])

    return {
        "executed_position": float(new_position),
        "fill_price": fill_price,
        "transaction_cost": float(transaction_cost),
        "slippage_cost": float(slippage_cost),
    }
