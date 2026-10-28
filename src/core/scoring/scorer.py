"""Blotter in, score tuple out. Nothing dataset-specific.

**Strategies may not import this module, nor `src.core.scoring.metrics`.** If a strategy
can compute its own objective it can optimize a number it controls, so the
prohibition is enforced at import time rather than asked for in a prompt --
importing from anywhere under `src.strategies` raises. A static check over the
source tree backs it up, for the case where a strategy defers the import to call
time.

The metric itself lives in `src.core.scoring.metrics`; this module is the fixed surface
the loop calls and the file the permission tiers name.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import replace
from typing import Sequence

from . import metrics
from .metrics import (
    IDLE_CHARGE,
    MIN_DOWNSIDE_SESSIONS,
    MIN_SESSIONS,
    REJECT,
    Diagnostics,
    SessionSpan,
)
from ..types import Blotter, ScoreTuple

_FORBIDDEN_PREFIX = "src.strategies"


def _reject_import_from_strategies() -> None:
    """Walk the import stack; refuse if anything under src.strategies is on it."""
    for frame in inspect.stack():
        name = frame.frame.f_globals.get("__name__", "")
        if name == _FORBIDDEN_PREFIX or name.startswith(_FORBIDDEN_PREFIX + "."):
            raise ImportError(
                f"{name} may not import src.core.scoring.scorer. A strategy that can compute "
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
    idle_charge: float = IDLE_CHARGE,
    min_exposed_fraction: float | None = None,
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

    returns = metrics.session_returns(blotter, period, notional_base=notional_base,
                                      idle_charge=idle_charge)
    net = blotter.realized_pnl - blotter.commission
    trades = sum(1 for r in blotter.rows if r.realized_pnl != 0.0)
    # Amendment 6: exposed in too few of the scored sessions -> not scored at
    # all, the way MIN_SESSIONS already works. Default None so the conditional
    # null, the calibration rig and the power sweep -- which all call score() --
    # never reject inside a null; only run.py's scored path passes the floor.
    # Applied to the candidate once, here; the null resamples one that passed.
    exposed = -1
    if min_exposed_fraction is not None:
        # Computed once, before the branch, so the number reaches the row
        # whichever way the branch goes. It used to live inside the rejection
        # and the passing rows carried nothing.
        exposed = metrics.sessions_exposed(blotter, period)
        need = min_exposed_fraction * len(period)
        if exposed < need:
            return ScoreTuple(
                sessions_exposed=exposed,
                min_exposed_fraction=min_exposed_fraction,
                primary=REJECT,
                sharpe=0.0,
                skew=0.0,
                kurtosis=0.0,
                n_sessions=len(period),
                net_pnl=net,
                trade_count=trades,
                rejected=True,
                reject_reason=(
                    f"exposed in {exposed} of {len(period)} scored sessions, "
                    f"under the registered floor of {min_exposed_fraction:.0%} "
                    f"(Amendment 6)"),
            )
    out = score_returns(
        returns, net_pnl=net, trade_count=trades, min_sessions=min_sessions,
        min_downside_sessions=min_downside_sessions,
    )
    if min_exposed_fraction is None:
        return out
    return replace(out, sessions_exposed=exposed,
                   min_exposed_fraction=min_exposed_fraction)


def score_returns(
    returns: Sequence[float],
    *,
    net_pnl: float = 0.0,
    trade_count: int = 0,
    min_sessions: int = MIN_SESSIONS,
    min_downside_sessions: int = MIN_DOWNSIDE_SESSIONS,
) -> ScoreTuple:
    """The scoring rule itself, over a return series.

    Split out of `score` so the rule can be exercised on a constructed series --
    the Sortino/Sharpe boundary test needs two trials that differ only in how
    many sessions finished below the MAR, and building blotters that do that
    exactly is harder than it is informative. `score` is the same function with
    the returns derived from a blotter and a calendar.
    """
    n = len(returns)
    net = net_pnl
    trades = trade_count

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
    # Both ratios, always. The accept rule charges the worse of the two nulls
    # (`null.far_for_score`), which it can only do if both numbers survive.
    sortino = metrics.sortino(returns)
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
            sortino=sortino,
            downside_sessions=losers,
        )

    return ScoreTuple(
        primary=sortino,
        sharpe=sharpe,
        sortino=sortino,
        skew=metrics.skewness(returns),
        kurtosis=metrics.excess_kurtosis(returns),
        n_sessions=n,
        net_pnl=net,
        trade_count=trades,
        rejected=False,
        downside_sessions=losers,
    )


DROP_PROBES = (1, 2, 3, 5, 10)


def drop_best_fragility(
    returns: Sequence[float], *, max_k: int = 25,
    min_sessions: int = MIN_SESSIONS,
) -> tuple[int, dict[str, float | None]]:
    """Remove the k best sessions, rescore, and report when the result dies.

    Returns `(sessions_to_zero, primary_after_drop)`.

    `sessions_to_zero` is the smallest k >= 1 for which `primary` is <= 0 once
    the k highest-returning sessions are dropped:

      *  0  the result is already <= 0 or rejected with nothing dropped, so
            there is nothing to probe
      * -1  it survived every probe up to `max_k`, or the series is too short
            to drop anything without crossing the session floor

    In `primary_after_drop`, a value of `None` means the rescored series was
    REJECTED rather than merely weak -- usually its downside sessions fell
    below the pre-registered floor once the best sessions were gone.

    **Why <= 0 and not the accept threshold.** The gate's real decision is a
    max-of-N p-value against a null, and recomputing that per probe would mean
    rebuilding a null per probe. Zero is gate-independent, needs no
    calibration, and means the same thing for every metric and session count.
    A seat wanting a stricter bar reads `primary_after_drop` directly.

    Dropping shortens the series, so a probe that would take it under the
    pre-registered session floor is not run -- otherwise the floor's own
    rejection would read as fragility.

    This is what `profit_concentration_top5pct` gestures at, in a form that
    has a bar: a share invites an argument, an integer invites a decision. See
    `Diagnostics.sessions_to_zero` for the provenance.
    """
    base = score_returns(returns)
    if base.rejected or base.primary <= 0.0:
        return 0, {}

    ordered = sorted(returns, reverse=True)
    n = len(ordered)
    curve: dict[str, float | None] = {}
    hit = -1
    for k in range(1, min(max_k, n) + 1):
        if n - k < min_sessions:
            break
        s = score_returns(ordered[k:])
        if k in DROP_PROBES:
            curve[str(k)] = None if s.rejected else float(s.primary)
        if hit == -1 and (s.rejected or s.primary <= 0.0):
            hit = k
    return hit, curve


def score_diagnostics(
    blotter: Blotter,
    period: Sequence[SessionSpan],
    *,
    notional_base: float = 1.0,
    fill_model_spread: float | None = None,
    idle_charge: float = IDLE_CHARGE,
) -> Diagnostics:
    """Recorded per run, never terms in the objective. See `metrics.Diagnostics`."""
    returns = metrics.session_returns(blotter, period, notional_base=notional_base,
                                      idle_charge=idle_charge)
    d = metrics.diagnostics(
        blotter, period, returns, fill_model_spread=fill_model_spread
    )
    hit, curve = drop_best_fragility(returns)
    return replace(d, sessions_to_zero=hit, primary_after_drop=curve)
