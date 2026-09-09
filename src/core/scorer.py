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
import math
from typing import Sequence

from . import metrics
from .metrics import (
    MIN_DOWNSIDE_SESSIONS,
    MIN_SESSIONS,
    REJECT,
    Diagnostics,
    SessionSpan,
)
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
    min_downside_sessions: int = MIN_DOWNSIDE_SESSIONS,
) -> ScoreTuple:
    """Sortino on per-session returns, with Sharpe and the shape moments beside it.

    `period` is the session calendar of the split being scored -- every session in
    it counts, including the ones the strategy sat out. Passing none at all is a
    programming error rather than a licence to score on trades: without a
    calendar there is no observation unit, so the result is the reject sentinel.

    The fill-model spread is a diagnostic, not a term in the score, so it is
    computed by `fill_model_spread()` and recorded rather than passed in here --
    an argument this function accepted and never read.
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

    sharpe = metrics.sharpe(returns)
    losers = metrics.downside_count(returns)
    if not metrics.has_dispersion(returns) or not math.isfinite(sharpe):
        # Zero dispersion: every session returned the same thing. Sortino is
        # infinite and so is the fallback, so there is nothing left to threshold.
        # Reject rather than accept -- an unbounded score does not just pass one
        # bad candidate, it steers the search toward whatever produces it.
        # The tolerance is relative: floating-point noise makes an identical
        # series have a standard deviation near 1e-16 rather than zero, which is
        # finite and enormous instead of infinite and obvious.
        return ScoreTuple(
            primary=REJECT,
            sharpe=0.0,
            skew=0.0,
            kurtosis=0.0,
            n_sessions=n,
            net_pnl=net,
            trade_count=trades,
            rejected=True,
            reject_reason="per-session returns have no dispersion; no ratio is defined",
        )
    if losers < min_downside_sessions:
        # Too few below-MAR sessions to estimate downside deviation, and at zero
        # the ratio is infinite and an accept rule thresholding it accepts
        # unconditionally. Sharpe is always defined; the metric name changes with
        # it, so the fallback is scored against its own null rather than
        # Sortino's.
        return ScoreTuple(
            primary=sharpe,
            sharpe=sharpe,
            skew=metrics.skewness(returns),
            kurtosis=metrics.excess_kurtosis(returns),
            n_sessions=n,
            net_pnl=net,
            trade_count=trades,
            rejected=False,
            reject_reason=(
                f"{losers} below-MAR session(s) is under the pre-registered "
                f"{min_downside_sessions}; scored on Sharpe instead"
            ),
            metric="sharpe_per_session",
        )

    return ScoreTuple(
        primary=metrics.sortino(returns),
        sharpe=sharpe,
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
