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

import bisect
import inspect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..types import Blotter, BlotterRow, Ts

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

# Amendment 6. The fraction of scored sessions a candidate must have been
# exposed in to be scoreable at all. The DEFAULT HERE IS THE CONFIG'S, not the
# function's: `score()` takes `min_exposed_fraction=None` (no floor) so the
# conditional null, the calibration rig and the power sweep -- all of which
# call score() -- do not start rejecting inside a null. Only run.py's scored
# path passes the config value. Task 69 measured why the floor is needed: a
# candidate exposed in 14 of 100 sessions has its Sortino inflated ~2.7x by
# idle sessions padding the denominator; at 70 of 100 the bound is 1.20x.
MIN_EXPOSED_FRACTION = 0.70

# Pre-registration Amendment 3. A session in which no position was held is
# charged this many index points, pro-rated by the fraction of the session that
# WAS held. It is a cost of capital -- roughly $20,000 of initial margin at ~4.5%
# over 252 days at $20 a point -- and it exists because `MAR = 0` over the full
# calendar made sitting out *profitable*: `sortino` goes as `1/sqrt(n)` for a
# fixed set of trades, so padding the return vector with flat sessions drags a
# negative score toward zero. An agent optimising that learns to stop trading,
# which is exactly what `nq-mvp-01` (3 trades in 384 sessions) and
# `nq-orderflow-01` (8) did.
#
# Derived, not fitted. Plausible inputs -- margin $15k-$30k, rates 4-5% -- span
# 0.12 to 0.30, and the measured window in which the incentive points at
# participation without rewarding churn is 0.10 to 0.30. Re-derive if rates move;
# never re-fit to a result.
IDLE_CHARGE = 0.18

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
                f"{name} may not import src.core.scoring.metrics. The metric is the number "
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


def _held_intervals(rows: Sequence[BlotterRow], end_ts: Ts) -> list[tuple[Ts, Ts]]:
    """Intervals `[opened, flat)` during which a position was held.

    Unlike `_holding_periods`, which returns durations and **drops a position
    still open at the end of the data** -- its loop only appends on a return to
    flat. Dropping it would hand the strategy free exposure for every session
    after it stopped closing, so an open position is carried to `end_ts`.
    """
    # **Per asset, then unioned.** `position_after` is a per-asset number, so a
    # single pass over interleaved rows closes the interval the moment ANY book
    # returns to flat while the others are still held -- a conversion would read
    # as flat whenever one leg zeroed, and the exposure floor would reject a
    # package that never stopped being on. The book is exposed when any asset
    # is. With one asset this reduces to exactly the old loop, which is the
    # property that keeps every committed score unmoved.
    spans: list[tuple[Ts, Ts]] = []
    by_asset: dict[str, list[BlotterRow]] = defaultdict(list)
    for row in rows:
        by_asset[row.asset].append(row)
    for asset_rows in by_asset.values():
        opened: Ts | None = None
        for row in asset_rows:
            if opened is None and row.position_after != 0:
                opened = row.ts
            elif opened is not None and row.position_after == 0:
                spans.append((opened, row.ts))
                opened = None
        if opened is not None:
            spans.append((opened, max(opened, end_ts)))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[Ts, Ts]]) -> list[tuple[Ts, Ts]]:
    """Overlapping intervals collapsed, so exposure is never double-counted.

    Two legs of one package held at the same time are one exposed interval,
    not two: `session_exposure` measures whether the book was on, not how many
    ways it was on.
    """
    if not spans:
        return []
    out: list[tuple[Ts, Ts]] = []
    for start, stop in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], stop))
        else:
            out.append((start, stop))
    return out


def session_exposure(
    blotter: Blotter,
    calendar: Sequence[SessionSpan],
) -> list[float]:
    """Fraction of each session during which a position was held, in [0, 1].

    **Time-weighted, and that is the point.** Counting "a session with at least
    one fill" as exposed -- which is what `sessions_with_exposure` reports -- lets
    one lot held for a minute stand in for a whole session, so a strategy can
    dodge the idle charge entirely while being effectively flat. At a 6.5-hour
    session that minute is 0.3% of the day and is charged 99.7% of the rate here.

    Exposure is clipped per session, deliberately unlike P&L, which
    `session_returns` attributes wholly to the session a position was closed in.
    That asymmetry is intended: P&L attribution is limited by what a blotter can
    support, exposure is not, and pretending otherwise would make an overnight
    hold free in the session it was carried through.

    Invariant under the sign-flip null -- flipping a round turn changes its P&L,
    never its timestamps -- which is why one vector is correct for the observed
    series and for every draw.
    """
    if not calendar:
        return []
    spans = sorted(calendar, key=lambda s: s.start_ts)
    intervals = _held_intervals(blotter.rows, spans[-1].end_ts)
    starts = [s.start_ts for s in spans]
    out = [0.0] * len(spans)
    for lo, hi in intervals:
        if hi <= lo:
            continue
        # Only the sessions this interval can touch, so a long hold over a long
        # calendar does not cost a full scan per interval.
        i = bisect.bisect_right(starts, lo) - 1
        if i < 0:
            i = 0
        while i < len(spans) and spans[i].start_ts <= hi:
            span = spans[i]
            a, b = max(lo, span.start_ts), min(hi, span.end_ts)
            if b > a:
                out[i] += float(b - a)
            i += 1
    for i, span in enumerate(spans):
        total = float(span.end_ts - span.start_ts)
        out[i] = min(1.0, out[i] / total) if total > 0 else 0.0
    order = {s.session: i for i, s in enumerate(spans)}
    return [out[order[s.session]] for s in calendar]


def position_scale(blotter: Blotter) -> float:
    """Peak contracts held, which is what the capital is sized for.

    **The idle charge is per contract, and it has to be.** A flat charge in index
    points is diluted by size: P&L scales with contracts and a fixed charge does
    not, so a strategy escapes the charge entirely by trading bigger -- at 100
    contracts a 0.18 charge moves the score from -0.0645 to -0.0676 instead of
    -0.3554. That is a new reward hack in place of the one being closed, and it
    is what `test_scaling_every_position_by_a_constant_does_not_change_the_score`
    exists to catch.

    Scaling by peak position restores that invariance exactly: double every
    position and P&L, margin and the charge all double, leaving the ratio alone.
    Peak rather than average because margin is posted for the largest position
    taken, not the typical one.

    **Position, not fill size.** The largest single fill was tried as a fallback
    for blotters that never record a non-zero position, and it is wrong: splitting
    one trade into two halves halves the largest fill without changing the capital
    committed, which broke trade-splitting invariance. Peak position is invariant
    to how a trade is chopped up, which is the property wanted here.

    A blotter that never records a non-zero position -- a synthetic P&L-only
    fixture, or a strategy that never traded -- has no scale to measure and gets
    1.0. For the strategy that never traded it changes nothing: every session is
    charged the same amount, the return vector has no dispersion, and
    `score_returns` rejects it at any scale.
    """
    # **The gross across the book at each instant, not the largest single
    # leg.** `position_after` is per asset, so the old max answered "the
    # biggest position any one book ever held" -- which for a hedged package of
    # three one-lot legs is 1, understating the scale by three and inflating
    # every per-unit figure derived from it. Walking the joint position gives
    # the same answer as before when there is one asset.
    live: dict[str, int] = {}
    peak = 0
    for r in blotter.rows:
        live[r.asset] = r.position_after
        gross = sum(abs(v) for v in live.values())
        if gross > peak:
            peak = gross
    return float(peak) if peak > 0 else 1.0


def idle_charges(
    blotter: Blotter,
    calendar: Sequence[SessionSpan],
    *,
    idle_charge: float = IDLE_CHARGE,
    notional_base: float = 1.0,
) -> list[float]:
    """The per-session charge vector: `-idle_charge * contracts * (1 - exposure)`.

    Computed once and used by both `session_returns` and the sign-flip null. A
    charge the observed series pays and the null does not moves a measured p from
    0.0367 to 0.1813, because the null is handed free upside on sessions that
    never carried a bet.
    """
    if idle_charge <= 0:
        return [0.0] * len(calendar)
    scale = position_scale(blotter)
    return [-idle_charge * scale * (1.0 - f) / notional_base
            for f in session_exposure(blotter, calendar)]


def sessions_exposed(blotter: Blotter, calendar: Sequence[SessionSpan]) -> int:
    """How many calendar sessions the strategy held a position in at all.

    The same time-weighted exposure the idle charge uses, counted as "any" per
    session. Diagnostics report it as `sessions_with_exposure`; Amendment 6's
    floor gates on it.
    """
    return sum(1 for e in session_exposure(blotter, calendar) if e > 0)


def session_returns(
    blotter: Blotter,
    calendar: Sequence[SessionSpan],
    *,
    notional_base: float = 1.0,
    idle_charge: float = IDLE_CHARGE,
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
    # Collected then summed with `math.fsum`, not accumulated with `+=`. A
    # running float total is order-deterministic and therefore platform-stable,
    # so this is not the cross-platform fix -- that was `Blotter.realized_pnl`
    # and the statistics in this module. It is here because a session return is
    # the number the whole gate is computed from, and an exactly-rounded sum
    # costs nothing.
    per: dict[int, list[float]] = {s.session: [] for s in calendar}
    for row in blotter.rows:
        s = _session_of(row.ts, calendar)
        if s is None:
            continue
        per[s].append(row.realized_pnl - row.commission)
    charges = idle_charges(blotter, calendar, idle_charge=idle_charge,
                           notional_base=notional_base)
    return [math.fsum(per[s.session]) / notional_base + c
            for s, c in zip(calendar, charges)]


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
    total = math.fsum(min(0.0, r - mar) ** 2 for r in returns)
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
    mean = math.fsum(returns) / n
    scale = max(abs(mean), max(abs(r) for r in returns), 1e-12)
    sd = math.sqrt(math.fsum((r - mean) ** 2 for r in returns) / (n - 1))
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
    mean = math.fsum(returns) / n
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
    mean = math.fsum(returns) / n
    var = math.fsum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)
    return mean / sd if sd > 0 else (float("inf") if mean > 0 else 0.0)


def _moment(returns: Sequence[float], k: int) -> float:
    n = len(returns)
    mean = math.fsum(returns) / n
    return math.fsum((r - mean) ** k for r in returns) / n


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
    total = math.fsum(gains)
    if total <= 0:
        return 0.0
    k = max(1, int(round(len(returns) * top_frac)))
    return math.fsum(gains[:k]) / total


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
    # The same P&L attributed to the hour the position was OPENED rather than
    # the hour it was closed out. See `compute_diagnostics` for why: a
    # hold-to-expiry strategy realises everything on an involuntary settlement
    # row, so `by_hour_utc` describes the expiry calendar and this one
    # describes the strategy.
    by_hour_utc_entry: dict[str, float] = field(default_factory=dict)
    fill_model_spread: float | None = None
    # How far this candidate's trading profile sits from the reference strategy's.
    #
    # Every threshold in the tabulated null is the distribution of
    # `ReferenceStrategy`'s per-session returns. A candidate that trades at a
    # different frequency, or holds for a different length of time, or has a
    # different return dispersion, has a *different* null -- and the table does
    # not know that. The proper fix is a per-candidate randomization null at the
    # committee's promotion gate; too expensive per trial, correct on promotion.
    #
    # Until then this is the visible version of the assumption: a candidate far
    # from the reference on these axes is a candidate whose FAR is least
    # trustworthy, and the committee can see that without recomputing anything.
    # It is a diagnostic and never a term in the score.
    null_reference_divergence: dict[str, float] = field(default_factory=dict)
    null_assumption: str = (
        "the tabulated null is the reference strategy's; a candidate far from it "
        "on null_reference_divergence has a null this table does not describe"
    )
    seasonality_caveat: str = (
        "descriptive, not inferential: at a few hundred sessions the per-cell "
        "counts in these breakdowns are tiny. **Read `by_hour_utc` against "
        "`by_hour_utc_entry` before calling anything concentrated.** The first "
        "buckets P&L at the instant it was REALISED, which for any position "
        "held across time is the close -- and for an option the close is a "
        "cash settlement at an expiry the strategy neither chose nor can "
        "prevent. The second buckets the same P&L at the hour the position was "
        "OPENED. Where they disagree, the first is describing the expiry "
        "calendar and the second is describing the strategy. Note also that an "
        "hour-of-day is only a market clock if the dataset has one: on the "
        "synthetic calendar a session is a few hours long and its start drifts "
        "across the day, so hour-of-day there is arithmetic on the session "
        "index and carries no regime information at all"
    )

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _holding_periods(rows: Sequence[BlotterRow]) -> list[int]:
    """Time from a position leaving flat to returning to flat."""
    # Per asset, for the same reason as `_held_intervals`: one book returning
    # to flat does not end another book's holding period. Durations are
    # reported per leg rather than unioned -- the question here is how long a
    # position was carried, and a two-leg package carried an hour is two
    # one-hour holds rather than one.
    out: list[int] = []
    by_asset: dict[str, list[BlotterRow]] = defaultdict(list)
    for row in rows:
        by_asset[row.asset].append(row)
    for asset_rows in by_asset.values():
        opened = None
        for row in asset_rows:
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
    reference_profile: dict | None = None,
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
    total_gain = math.fsum(gains)

    by_month: dict[str, float] = defaultdict(float)
    by_weekday: dict[str, float] = defaultdict(float)
    by_hour: dict[str, float] = defaultdict(float)
    # **The same P&L, attributed to the hour the position was OPENED.**
    #
    # `by_hour_utc` buckets a row's net at the row's own timestamp, and P&L on
    # anything held across time is realised on the CLOSING row. For an option
    # the close is a cash settlement at an expiry the strategy does not choose
    # and cannot prevent, so every point of a hold-to-expiry edge lands in
    # whichever hours the expiry calendar happens to fall in. spx-arb-01 was
    # vetoed by the robustness seat for "few periods carry the result" on
    # exactly that: 95% of its settlement fills sat in four hours, while its
    # own entries were spread across seventeen.
    #
    # This table answers the question the seat is actually asking -- in which
    # hours did the DECISIONS that earned this live -- and it needs no
    # provenance flag to do it, which matters: the obvious alternative is to
    # key off `tag == "settlement"`, and a tag is strategy-settable free text
    # with no validation, so a candidate could hide real concentration by
    # labelling its entries as settlements.
    entry_hour: dict[str, str] = {}
    held: dict[str, int] = {}
    by_hour_entry: dict[str, float] = defaultdict(float)
    for row in rows:
        when = dt.datetime.fromtimestamp(row.ts / 1e9, dt.timezone.utc)
        net = row.realized_pnl - row.commission
        by_month[when.strftime("%Y-%m")] += net
        by_weekday[when.strftime("%a")] += net
        by_hour[f"{when.hour:02d}"] += net

        this_hour = f"{when.hour:02d}"
        asset = row.asset
        if held.get(asset, 0) == 0 and row.position_after != 0:
            entry_hour[asset] = this_hour       # flat -> open: this is the entry
        by_hour_entry[entry_hour.get(asset, this_hour)] += net
        held[asset] = row.position_after
        if row.position_after == 0:
            entry_hour.pop(asset, None)

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
        by_hour_utc_entry=dict(sorted(by_hour_entry.items())),
        fill_model_spread=fill_model_spread,
        null_reference_divergence=null_reference_divergence(
            n_sessions=len(calendar),
            sessions_with_exposure=len(per_session_rows),
            trade_count=len(closes),
            avg_holding_period_ns=(sum(holds) / len(holds)) if holds else 0.0,
            returns=returns,
            reference=reference_profile,
        ),
    )


# The reference strategy's own profile, measured on the pool the null was built
# from. Written here rather than read from the null table so a run does not have
# to load it, and updated when the apparatus changes -- which the test asserts.
REFERENCE_PROFILE = {
    # Measured on the NQ training pool the null was built from: 384 sessions,
    # 33,390 fills, so 43.5 round trips a session, and a pooled session-return
    # standard deviation of 275. The holding period is the reference's mean,
    # roughly half an hour. These are the *scale* the ratios are read against,
    # not thresholds -- a test asserts they still describe the committed pool, so
    # a change in the apparatus shows up here rather than silently rebasing every
    # divergence number in the ledger.
    "trades_per_session": 43.5,
    "exposure_share": 1.0,
    "avg_holding_period_ns": 1.70e12,
    "return_dispersion": 275.0,
}


def buy_and_hold(dataset, split, *, commission_per_contract: float = 0.0,
                 notional_base: float = 1.0) -> dict:
    """One contract, bought at the split's first price, held to its last.

    **Committee evidence, deliberately NOT a gate and NOT shown to the
    proposer.**

    Not a gate, because the bar's height is set by the sample's realised drift
    rather than by anything about the strategy. Measured on real NQ in rolling
    100-session windows of the same data, buy-and-hold's Sortino runs from
    -0.0705 to +0.3309 and changes sign: trivial in a flat or falling stretch,
    a genuine hurdle in sessions 200-300. A pass/fail rule on that asks a
    different question of every campaign.

    Not shown to the proposer, for two reasons. It is targetable -- an agent
    told the benchmark can simply take the exposure rather than beat it, and
    "went long in a rising market" is the result this is meant to expose. And
    `docs/agent/*.md` is in ARTIFACT_GLOBS, so putting it in the prompt would
    open a new experimental regime to add a number the agent must not use.

    What it IS for: the economics seat currently cannot see the opportunity
    cost of a campaign. "+9,937 points while holding one contract would have
    made +5,257 with a 5,073-point drawdown" is exactly the judgement a seat
    exists to make, and judgement is the right instrument for a benchmark this
    sample-dependent.

    Reads through `dataset.ticks(split)`, so the holdout lock applies. A
    benchmark that bypassed it to look at holdout prices would be a look,
    whatever it was computed for.
    """
    import datetime as dt

    last_of: dict[int, float] = {}
    first_price: float | None = None
    for tick in dataset.ticks(split):
        if first_price is None:
            first_price = float(tick.price)
        last_of[tick.session] = float(tick.price)
    if first_price is None or not last_of:
        return {"available": False,
                "note": "the split served no ticks, so there is nothing to hold"}

    closes = [last_of[s] for s in sorted(last_of)]
    prev = first_price
    returns: list[float] = []
    for c in closes:
        returns.append((c - prev) / notional_base)
        prev = c
    # One round turn for the whole hold: in at the start, out at the end.
    cost = 2.0 * commission_per_contract
    if returns:
        returns[-1] -= cost / notional_base

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in returns:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    # The gate's own statistic, so the comparison is like with like. Imported
    # here because `scorer` imports this module.
    from .scorer import score_returns

    scored = score_returns(returns)
    return {
        "available": True,
        "n_sessions": len(returns),
        "pnl": math.fsum(returns),
        "primary": scored.primary,
        "entry_price": first_price,
        "exit_price": closes[-1],
        "max_drawdown": max_dd,
        "note": ("one contract held across the whole split, costed once. "
                 "EVIDENCE, not a gate: measured on NQ, this benchmark's "
                 "Sortino ranges -0.0705 to +0.3309 across rolling windows of "
                 "the same data and changes sign, so its difficulty is the "
                 "sample's drift and not the strategy's merit"),
    }


def null_reference_divergence(
    *,
    n_sessions: int,
    sessions_with_exposure: int,
    trade_count: int,
    avg_holding_period_ns: float,
    returns: Sequence[float],
    reference: dict | None = None,
) -> dict[str, float]:
    """How far this candidate's trading profile sits from the reference's.

    Ratios, not differences, and log-ratios would be tidier but harder to read at
    a glance; 1.0 means "the same as the reference". The point is not the exact
    number, it is that a candidate trading twice a session against the
    reference's eighty-eight has a null this table does not describe, and that
    fact should be visible in the run record rather than inferred later by
    someone reading the code.
    """
    ref = reference or REFERENCE_PROFILE
    if n_sessions <= 0:
        return {}
    sd = _sd(returns)

    def ratio(value: float, base: float) -> float:
        if base <= 0:
            return float("inf") if value > 0 else 1.0
        return round(value / base, 4)

    return {
        "trades_per_session": ratio(trade_count / n_sessions,
                                    ref["trades_per_session"]),
        "exposure_share": ratio(sessions_with_exposure / n_sessions,
                                ref["exposure_share"]),
        "avg_holding_period": ratio(avg_holding_period_ns,
                                    ref["avg_holding_period_ns"]),
        "return_dispersion": ratio(sd, ref["return_dispersion"]),
    }


def _sd(values: Sequence[float]) -> float:
    vals = [float(v) for v in values]
    if len(vals) < 2:
        return 0.0
    mean = math.fsum(vals) / len(vals)
    return math.sqrt(math.fsum((v - mean) ** 2 for v in vals) / (len(vals) - 1))


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
    mean = math.fsum(trades) / n
    sd = math.sqrt(math.fsum((t - mean) ** 2 for t in trades) / (n - 1))
    return (mean / sd * math.sqrt(n) if sd > 0 else 0.0), n, net
