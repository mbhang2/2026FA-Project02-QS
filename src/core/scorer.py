"""Blotter in, score tuple out. Nothing dataset-specific.

**Strategies may not import this module.** If a strategy can compute its own
objective it can optimize a number it controls, so the prohibition is enforced at
import time rather than asked for in a prompt -- importing from anywhere under
`src.strategies` raises. A static check over the source tree backs it up, for the
case where a strategy defers the import to call time.

**[open] The metric.** It must be invariant to turnover, leverage, universe size
*and decision frequency*: candidates may subscribe to different timeframes and
produce trade counts differing by orders of magnitude, and a metric that rewards
trading more is a metric the agent will satisfy by trading more. What is here is
a placeholder with a fixed interface -- net P&L after costs, trade count, and a
vol-normalized per-trade return with its t-statistic.
"""

from __future__ import annotations

import inspect
import math
import sys

from .types import Blotter, ScoreTuple

_FORBIDDEN_PREFIX = "src.strategies"


def _reject_import_from_strategies() -> None:
    """Walk the import stack; refuse if anything under src.strategies is on it."""
    for frame in inspect.stack():
        name = frame.frame.f_globals.get("__name__", "")
        if name == _FORBIDDEN_PREFIX or name.startswith(_FORBIDDEN_PREFIX + "."):
            raise ImportError(
                f"{name} may not import src.core.scorer. A strategy that can compute "
                "its own objective can optimize a number it controls; the harness "
                "calls the scorer, the strategy does not."
            )


_reject_import_from_strategies()


def score(blotter: Blotter, *, period: object | None = None) -> ScoreTuple:
    """Placeholder metric over a blotter.

    Per-trade returns are the realized P&L of each position-reducing fill, net of
    the commission on both legs, expressed in price units per contract so the
    number does not scale with size -- one of the invariances the real metric
    will need.
    """
    trades: list[float] = []
    commission_total = 0.0
    for row in blotter.rows:
        commission_total += row.commission
        if row.realized_pnl != 0.0 or (row.position_after == 0 and row.size):
            if row.size:
                trades.append((row.realized_pnl - row.commission) / row.size)

    net = blotter.realized_pnl - commission_total
    n = len(trades)
    if n == 0:
        return ScoreTuple(net, 0, 0.0, 0.0, 0.0)

    mean = sum(trades) / n
    if n > 1:
        var = sum((t - mean) ** 2 for t in trades) / (n - 1)
        sd = math.sqrt(var)
    else:
        sd = 0.0
    vol_norm = mean / sd if sd > 0 else 0.0
    t_stat = vol_norm * math.sqrt(n) if sd > 0 else 0.0
    return ScoreTuple(
        net_pnl=net,
        trade_count=n,
        mean_return_per_trade=mean,
        vol_normalized_return=vol_norm,
        t_stat=t_stat,
    )
