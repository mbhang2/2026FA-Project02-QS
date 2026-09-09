"""Blotter in, score tuple out. Nothing dataset-specific.

**Strategies may not import this module, nor `src.core.metrics`.** If a strategy
can compute its own objective it can optimize a number it controls, so the
prohibition is enforced at import time rather than asked for in a prompt --
importing from anywhere under `src.strategies` raises. A static check over the
source tree backs it up, for the case where a strategy defers the import to call
time.

The metric itself lives in `src.core.metrics`; this module is the fixed surface
the loop calls and the file the permission tiers name.
"""

from __future__ import annotations

import inspect
from typing import Sequence

from . import metrics
from .metrics import MIN_SESSIONS, REJECT, Diagnostics, SessionSpan
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


def score(
    blotter: Blotter,
    *,
    period: Sequence[SessionSpan] | None = None,
    notional_base: float = 1.0,
    min_sessions: int = MIN_SESSIONS,
    fill_model_spread: float | None = None,
) -> ScoreTuple:
    """Sortino on per-session returns, with Sharpe and the shape moments beside it.

    `period` is the session calendar of the split being scored -- every session in
    it counts, including the ones the strategy sat out. Passing none at all is a
    programming error rather than a licence to score on trades: without a
    calendar there is no observation unit, so the result is the reject sentinel.
    """
    if not period:
        return ScoreTuple(
            primary=REJECT,
            sharpe=0.0,
            skew=0.0,
            kurtosis=0.0,
            n_sessions=0,
            net_pnl=blotter.realized_pnl - blotter.commission,
            trade_count=0,
            rejected=True,
            reject_reason="no session calendar supplied; per-session returns are undefined",
        )

    returns = metrics.session_returns(blotter, period, notional_base=notional_base)
    n = len(returns)
    net = blotter.realized_pnl - blotter.commission
    trades = sum(1 for r in blotter.rows if r.realized_pnl != 0.0)

    if n < min_sessions:
        return ScoreTuple(
            primary=REJECT,
            sharpe=metrics.sharpe(returns),
            skew=metrics.skewness(returns),
            kurtosis=metrics.excess_kurtosis(returns),
            n_sessions=n,
            net_pnl=net,
            trade_count=trades,
            rejected=True,
            reject_reason=f"{n} sessions is below the pre-registered floor of {min_sessions}",
        )

    return ScoreTuple(
        primary=metrics.sortino(returns),
        sharpe=metrics.sharpe(returns),
        skew=metrics.skewness(returns),
        kurtosis=metrics.excess_kurtosis(returns),
        n_sessions=n,
        net_pnl=net,
        trade_count=trades,
        rejected=False,
    )


def score_diagnostics(
    blotter: Blotter,
    period: Sequence[SessionSpan],
    *,
    notional_base: float = 1.0,
    fill_model_spread: float | None = None,
) -> Diagnostics:
    """Recorded per run, never terms in the objective. See `metrics.Diagnostics`."""
    returns = metrics.session_returns(blotter, period, notional_base=notional_base)
    return metrics.diagnostics(
        blotter, period, returns, fill_model_spread=fill_model_spread
    )
