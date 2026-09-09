"""Turnover and PnL concentration metrics (Task 3.4)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def turnover(positions: pd.Series) -> float:
    """Average absolute position change per bar, including the initial entry.

    A constant-position strategy scores near zero; a strategy that flips every
    bar scores near its full position magnitude.
    """
    if len(positions) == 0:
        return 0.0
    pos = np.asarray(positions, dtype=float)
    changes = np.abs(np.diff(np.concatenate(([0.0], pos))))
    return float(changes.mean())


def pnl_concentration(daily_returns: pd.Series) -> float:
    """Fraction of total positive PnL contributed by the single best day.

    Flags a strategy whose entire edge comes from one lucky bar. Returns 0.0 if
    there are no positive returns.
    """
    returns = np.asarray(daily_returns, dtype=float)
    positive = returns[returns > 0.0]
    total = float(positive.sum())
    if total == 0.0:
        return 0.0
    return float(positive.max() / total)
