"""Return metrics (Task 3.1)."""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def cumulative_return(equity: pd.Series) -> float:
    """Total return over the full equity curve: ``equity[-1] / equity[0] - 1``."""
    if len(equity) == 0:
        return 0.0
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)


def sharpe_ratio(daily_returns: pd.Series, risk_free: float = 0.0) -> float:
    """Annualized Sharpe ratio from daily returns.

    Uses the sample standard deviation (``ddof=1``) and ``sqrt(252)``
    annualization. Degenerate (zero-variance) series return 0.0.
    """
    returns = np.asarray(daily_returns, dtype=float)
    if len(returns) < 2:
        return 0.0
    std = returns.std(ddof=1)
    if std == 0.0:
        return 0.0
    excess = returns.mean() - risk_free / TRADING_DAYS
    return float(excess / std * np.sqrt(TRADING_DAYS))


def annualized_volatility(daily_returns: pd.Series) -> float:
    returns = np.asarray(daily_returns, dtype=float)
    if len(returns) < 2:
        return 0.0
    return float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS))
