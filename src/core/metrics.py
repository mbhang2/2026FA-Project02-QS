"""The scoring metric and its diagnostics. Pure functions over a blotter and a
session calendar.

**Strategies may not import this module**, for the same reason they may not
import the scorer: a strategy that can compute its own objective can optimize a
number it controls.

The placeholder this replaces was a t-statistic on per-trade P&L, and the
calibration sweep proved it broken — acceptance *fell* as the planted edge got
stronger while net P&L kept climbing. Per-trade statistics are not invariant to
decision frequency, and the per-trade dispersion of this strategy grows with the
signal faster than its mean does. `placeholder_score` is kept so that curve stays
reproducible rather than merely archived.

Conventions, all deliberate and all stated in `docs/design/metric.md`:

* returns are **per session**, never per trade — a strategy trading fifty times a
  day and one trading once a day then have comparable observations, which is also
  what the DSR and PBO literature assumes;
* **MAR = 0**;
* downside deviation divides by **all N observations**, not the count of losing
  ones — the latter shrinks as a strategy improves and makes Sortino explode on a
  handful of losses;
* every session in the split counts, **including flat ones** — a strategy holding
  no position still ties up capital, and conditioning on days-it-traded makes
  three lucky trades look extraordinary;
* a **constant** notional base, so the ratio cannot be moved by scaling size.
"""

from __future__ import annotations

import inspect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .types import Blotter, BlotterRow, Ts

# Pre-registered, not tuned: below this many sessions the ratio is not a
# measurement and the loop must treat the trial as a reject rather than accept a
# large number computed from a handful of observations.
MIN_SESSIONS = 30

# Sortino needs losses to have a denominator. Below this many below-MAR sessions
# the downside deviation is estimated from too few points, and at zero it is not
# estimable at all -- the ratio is then genuinely infinite and an accept rule
# that thresholds it accepts unconditionally. An unbounded auto-accept does not
# just pass one bad candidate; it steers the search toward whatever produces it.
# Below the floor the score falls back to Sharpe, which is always defined, and
# the fallback path carries its own name so it is calibrated against its own
# null rather than Sortino's.
MIN_DOWNSIDE_SESSIONS = 5

# The sentinel. -inf rather than None so any comparison against a threshold
# rejects, however careless the caller.
REJECT = float("-inf")

MAR = 0.0

_FORBIDDEN_PREFIX = "src.strategies"


def _reject_import_from_strategies() -> None:
    for frame in inspect.stack():
        name = frame.frame.f_globals.get("__name__", "")
        if name == _FORBIDDEN_PREFIX or name.startswith(_FORBIDDEN_PREFIX + "."):
            raise ImportError(
                f"{name} may not import src.core.metrics. The metric is the number "
                "the agent is graded on; a strategy that can compute it can optimize "
                "a number it controls."
            )


_reject_import_from_strategies()


@dataclass(frozen=True, slots=True)
class SessionSpan:
    session: int
    start_ts: Ts
    end_ts: Ts


def session_calendar(dataset, sessions: Iterable[int]) -> list[SessionSpan]:
    out = []
    for s in sessions:
        start, end = dataset.session_bounds(s)
        out.append(SessionSpan(s, start, end))
    return sorted(out, key=lambda x: x.start_ts)


def session_returns(
    blotter: Blotter,
    calendar: Sequence[SessionSpan],
    *,
    notional_base: float = 1.0,
) -> list[float]:
    """One observation per session in the calendar, flat sessions included.

    P&L is attributed to the session the closing fill happened in, which is what
    a blotter can support: an overnight hold's P&L lands in the session it is
    closed, not spread across the ones it was carried through. The count of
    sessions ending with a position open is reported as a diagnostic so that
    distortion is visible rather than silent.
    """
    if notional_base <= 0:
        raise ValueError("notional_base must be positive")
    per: dict[int, float] = {s.session: 0.0 for s in calendar}
    for row in blotter.rows:
        s = _session_of(row.ts, calendar)
        if s is None:
            continue
        per[s] += row.realized_pnl - row.commission
    return [per[s.session] / notional_base for s in calendar]


def _session_of(ts: Ts, calendar: Sequence[SessionSpan]) -> int | None:
    lo, hi = 0, len(calendar) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        span = calendar[mid]
        if ts < span.start_ts:
            hi = mid - 1
        elif ts > span.end_ts:
            lo = mid + 1
        else:
            return span.session
    # A fill stamped in a gap between sessions belongs to the session that just
    # ended; the engine only fills on ticks, so this is defensive.
    return calendar[hi].session if hi >= 0 else None


# --------------------------------------------------------------------------- #
# the statistics
# --------------------------------------------------------------------------- #
def downside_deviation(returns: Sequence[float], mar: float = MAR) -> float:
    """Standard form: the sum of squared shortfalls over **all** N.

    Dividing by the number of losing observations instead is the classic Sortino
    pathology -- the denominator shrinks as a strategy improves, so the ratio
    explodes on a handful of losses and the acceptance curve stops being monotone.
    """
    n = len(returns)
    if n == 0:
        return 0.0
    total = sum(min(0.0, r - mar) ** 2 for r in returns)
    return math.sqrt(total / n)


def has_dispersion(returns: Sequence[float], rel_tol: float = 1e-9) -> bool:
    """Whether the return series varies at all, to within floating-point noise.

    An exact equality test is not enough: forty sessions that each returned the
    same 0.98 have a standard deviation of about 1e-16 rather than zero, and a
    ratio computed from that is 1e15 -- finite, so an `isfinite` guard passes it,
    and large enough to clear any threshold. The tolerance is relative to the
    size of the returns so it does not depend on the units.
    """
    n = len(returns)
    if n < 2:
        return False
    mean = sum(returns) / n
    scale = max(abs(mean), max(abs(r) for r in returns), 1e-12)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (n - 1))
    return sd > rel_tol * scale


def downside_count(returns: Sequence[float], mar: float = MAR) -> int:
    return sum(1 for r in returns if r < mar)


def sortino(returns: Sequence[float], mar: float = MAR) -> float:
    """The ratio itself, unguarded. `scorer.score` applies the downside floor.

    Kept unguarded here because the null tabulation needs the raw statistic, and
    because a guard belongs where the decision is made rather than inside the
    definition of the statistic.
    """
    n = len(returns)
    if n == 0:
        return REJECT
    mean = sum(returns) / n
    dd = downside_deviation(returns, mar)
    if dd == 0.0:
        # No session finished below the MAR. Genuine at large N, almost always a
        # sample-size artefact at small N -- which is what the floor is for.
        return float("inf") if mean > mar else 0.0
    return (mean - mar) / dd


def sharpe(returns: Sequence[float]) -> float:
    """Same series, computed because DSR and PBO are defined for Sharpe. If the
    loop's scalar is Sortino and nothing computes Sharpe, the committee has
    nothing to deflate."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)
    return mean / sd if sd > 0 else (float("inf") if mean > 0 else 0.0)


def _moment(returns: Sequence[float], k: int) -> float:
    n = len(returns)
    mean = sum(returns) / n
    return sum((r - mean) ** k for r in returns) / n


def skewness(returns: Sequence[float]) -> float:
    n = len(returns)
    if n < 3:
        return 0.0
    m2, m3 = _moment(returns, 2), _moment(returns, 3)
    return m3 / m2**1.5 if m2 > 0 else 0.0


def excess_kurtosis(returns: Sequence[float]) -> float:
    n = len(returns)
    if n < 4:
        return 0.0
    m2, m4 = _moment(returns, 2), _moment(returns, 4)
    return (m4 / m2**2 - 3.0) if m2 > 0 else 0.0


def max_drawdown(returns: Sequence[float]) -> float:
    """On the cumulative return path, in return units. Reported, never scored."""
    peak = cum = 0.0
    worst = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def profit_concentration(returns: Sequence[float], top_frac: float = 0.05) -> float:
    """Share of gross profit coming from the best `top_frac` of sessions.

    Near 1.0 means the edge is a handful of days. A veto threshold at the
    committee, not a term in the objective.
    """
    gains = sorted((r for r in returns if r > 0), reverse=True)
    total = sum(gains)
    if total <= 0:
        return 0.0
    k = max(1, int(round(len(returns) * top_frac)))
    return sum(gains[:k]) / total


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #
@dataclass
class Diagnostics:
    """Recorded per run, never terms in the score.

    They become pre-registered veto thresholds at the committee's robustness
    auditor, which gives them teeth while keeping the loop's accept rule a single
    scalar and staying out of multi-objective optimization.
    """

    n_sessions: int = 0
    sessions_with_exposure: int = 0
    trade_count: int = 0
    win_rate: float = 0.0
    max_drawdown: float = 0.0
    profit_concentration_top5pct: float = 0.0
    best_session_share: float = 0.0
    avg_holding_period_ns: float = 0.0
    turnover_contracts_per_session: float = 0.0
    gross_pnl: float = 0.0
    commission: float = 0.0
    by_month: dict[str, float] = field(default_factory=dict)
    by_weekday: dict[str, float] = field(default_factory=dict)
    by_hour_utc: dict[str, float] = field(default_factory=dict)
    fill_model_spread: float | None = None
    seasonality_caveat: str = (
        "descriptive, not inferential: at a few hundred sessions the per-cell "
        "counts in these breakdowns are tiny"
    )

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _holding_periods(rows: Sequence[BlotterRow]) -> list[int]:
    """Time from a position leaving flat to returning to flat."""
    out, opened = [], None
    for row in rows:
        if opened is None and row.position_after != 0:
            opened = row.ts
        elif opened is not None and row.position_after == 0:
            out.append(row.ts - opened)
            opened = None
    return out


def diagnostics(
    blotter: Blotter,
    calendar: Sequence[SessionSpan],
    returns: Sequence[float],
    *,
    fill_model_spread: float | None = None,
) -> Diagnostics:
    import datetime as dt

    rows = blotter.rows
    per_session_rows: dict[int, list[BlotterRow]] = defaultdict(list)
    for row in rows:
        s = _session_of(row.ts, calendar)
        if s is not None:
            per_session_rows[s].append(row)

    closes = [r for r in rows if r.realized_pnl != 0.0]
    wins = sum(1 for r in closes if r.realized_pnl - r.commission > 0)
    holds = _holding_periods(rows)
    gains = sorted((r for r in returns if r > 0), reverse=True)
    total_gain = sum(gains)

    by_month: dict[str, float] = defaultdict(float)
    by_weekday: dict[str, float] = defaultdict(float)
    by_hour: dict[str, float] = defaultdict(float)
    for row in rows:
        when = dt.datetime.fromtimestamp(row.ts / 1e9, dt.timezone.utc)
        net = row.realized_pnl - row.commission
        by_month[when.strftime("%Y-%m")] += net
        by_weekday[when.strftime("%a")] += net
        by_hour[f"{when.hour:02d}"] += net

    return Diagnostics(
        n_sessions=len(calendar),
        sessions_with_exposure=len(per_session_rows),
        trade_count=len(closes),
        win_rate=(wins / len(closes)) if closes else 0.0,
        max_drawdown=max_drawdown(returns),
        profit_concentration_top5pct=profit_concentration(returns),
        best_session_share=(gains[0] / total_gain) if total_gain > 0 else 0.0,
        avg_holding_period_ns=(sum(holds) / len(holds)) if holds else 0.0,
        turnover_contracts_per_session=(
            sum(r.size for r in rows) / len(calendar) if calendar else 0.0
        ),
        gross_pnl=blotter.realized_pnl,
        commission=blotter.commission,
        by_month=dict(sorted(by_month.items())),
        by_weekday=dict(sorted(by_weekday.items())),
        by_hour_utc=dict(sorted(by_hour.items())),
        fill_model_spread=fill_model_spread,
    )


# --------------------------------------------------------------------------- #
# the placeholder, kept so its broken curve stays reproducible
# --------------------------------------------------------------------------- #
def placeholder_score(blotter: Blotter) -> tuple[float, int, float]:
    """t-statistic on per-trade P&L: the metric Task 3 shipped and Task 4
    replaced. Returns (t_stat, trade_count, net_pnl).

    Kept callable rather than deleted: the contrast between its acceptance curve
    and the new one is the evidence that the calibration rig catches metric
    pathologies and not only gate pathologies.
    """
    trades = [
        (r.realized_pnl - r.commission) / r.size
        for r in blotter.rows
        if r.size and (r.realized_pnl != 0.0 or r.position_after == 0)
    ]
    net = blotter.realized_pnl - blotter.commission
    n = len(trades)
    if n < 2:
        return 0.0, n, net
    mean = sum(trades) / n
    sd = math.sqrt(sum((t - mean) ** 2 for t in trades) / (n - 1))
    return (mean / sd * math.sqrt(n) if sd > 0 else 0.0), n, net
