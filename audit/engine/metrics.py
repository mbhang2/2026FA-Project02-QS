"""Backtest metrics. All inputs are aligned series of the same length."""

from __future__ import annotations

import math


def total_return(pnl: list[float]) -> float:
    wealth = 1.0
    for r in pnl:
        wealth *= 1.0 + r
    return wealth - 1.0


def sharpe(pnl: list[float], *, periods_per_year: int = 252) -> float:
    if len(pnl) < 2:
        return 0.0
    mean = sum(pnl) / len(pnl)
    var = sum((x - mean) ** 2 for x in pnl) / (len(pnl) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def max_drawdown(pnl: list[float]) -> float:
    peak = 1.0
    wealth = 1.0
    dd = 0.0
    for r in pnl:
        wealth *= 1.0 + r
        if wealth > peak:
            peak = wealth
        if peak > 0:
            dd = min(dd, wealth / peak - 1.0)
    return dd


def turnover(positions: list[float]) -> float:
    if len(positions) < 2:
        return 0.0
    changes = [abs(positions[i] - positions[i - 1]) for i in range(1, len(positions))]
    return sum(changes) / (len(positions) - 1)
