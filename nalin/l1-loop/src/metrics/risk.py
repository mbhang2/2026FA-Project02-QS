"""Risk metrics (Task 3.2)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    """Maximum peak-to-trough decline, returned as a positive magnitude.

    Monotonically increasing equity has zero drawdown.
    """
    if len(equity) == 0:
        return 0.0
    running_max = np.maximum.accumulate(np.asarray(equity, dtype=float))
    drawdowns = (running_max - np.asarray(equity, dtype=float)) / running_max
    return float(np.max(drawdowns))


def cvar(daily_returns: pd.Series, alpha: float = 0.05) -> float:
    """Conditional Value-at-Risk / expected shortfall.

    Returns a positive magnitude: the average loss within the worst ``alpha``
    fraction of returns, expressed as ``-mean(tail)``. The tail is the
    ``ceil(alpha * n)`` smallest returns (order-statistic definition, no
    interpolation).
    """
    returns = np.sort(np.asarray(daily_returns, dtype=float))
    if len(returns) == 0:
        return 0.0
    k = max(1, int(np.ceil(alpha * len(returns))))
    tail = returns[:k]
    return float(-tail.mean())
