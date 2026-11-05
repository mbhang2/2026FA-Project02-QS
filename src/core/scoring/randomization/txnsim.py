"""Round turn trade Monte Carlo: random traders with the candidate's own style.

A port of `blotter::txnsim` (Jasen Mackie and Brian G. Peterson), the method
chapter 24 of the quantstrat book calls *Round Turn Trade Monte Carlo*. It
learns a strategy's stylized facts from its round turns and builds replicate
strategies that share them but have no skill -- *"what would a strategy that
trades just like the backtest from the viewpoint of an outside observer, but
without the backtest's indicators, signals, and rules, look like?"*

**Not the sign-flip.** `round_turn.py` is also "Monte Carlo over round turns"
in this repo, and the name is the only thing they share. The sign-flip keeps
WHEN and WHICH trades and randomises direction, so it cannot see timing skill.
This keeps direction, size and duration and randomises when, which is the
question the book means by skill versus luck.

## Two modes

`mode="layered"` (the default) is the book's outline in section 24.15: a first
layer that **preserves the long/short/flat ratios**, with the candidate's
layering on top and its maximum position respected. `mode="flat.to.flat"` is
blotter's simpler `tradeDef`, which keeps only each round turn's opening size.

### What both port exactly

The stylized facts are `perTradeStats(tradeDef = 'flat.to.flat',
includeFlatPeriods = TRUE)`:

* a round turn runs from the fill that leaves flat to the fill that returns to
  flat, and a flip through zero does not end it (`round_turn.round_turns`
  agrees); its direction and quantity are `Init.Qty`, the signed opening fill;
* a flat period runs from one return-to-flat to the next opening fill; the
  initial flat period is dropped and a trailing one is kept to the end of the
  series;
* rows are laid end to end from the first observed opening, zero-duration rows
  are removed, and every fill is at the then-current price -- `findInterval`,
  the last trade at or before the instant -- with a close sorted before an
  open at the same timestamp.

`flat.to.flat` with `replacement = TRUE` is `idxexpr.wr`: rows (trades AND flat
periods) drawn with replacement in batches of `round(n * 1.1)`, then
`round(remaining / mean * 1.1)`, until the summed duration reaches the observed
total, the overshooting row trimmed and anything after it dropped. With
`replacement = FALSE` it is `idxexpr.nr`, one permutation of the rows.

### The first layer, ported from `tradesample`

`layered` builds its first layer the way `tradesample` does for
`increased.to.reduced`:

1. flat periods, long round turns and short round turns are each sampled with
   replacement, by the same batch loop (`subsample`), until each reaches its
   own observed total duration -- so the flat total is matched exactly;
2. long rows are taken in sampled order while their running duration stays
   under `round((calendar - flat) * long / (long + short))`, and short rows
   while it stays under what is left of the calendar;
3. the layer is shuffled and laid end to end.

Two R behaviours are deliberately not copied. `subsample`'s batch size divides
by the mean duration of ALL rows, not of the class being sampled; that IS
copied, because it is the algorithm. But `1:k` with `k = 0` is `c(1, 0)` in R
and selects a row, and `sample(x, n)` on a length-one `x` samples from `1:x`;
both are indexing accidents, and here an empty selection is empty and a single
row is that row. `calendar` is, as in blotter, first opening to last close, so
the trailing flat period counts in `flat` but not in `calendar`.

### The layers: simpler than blotter, on purpose

Blotter's own layering stacks extra trades onto random points of the first
layer under the maximum-position limit. Its code carries open TODOs ("double
check that all long and short trades have been allocated", "test for maximum
position?") and gives up after 1,000 attempts, so a replicate can end up with
fewer layers than the strategy had and nothing says so.

`layered` keeps each round turn's **position profile** instead: every add,
partial exit and flip, at its offset from the turn's start. A replicate places
the whole profile at a random time. Adds, their sizes and timing within the
trade, flips and the maximum position are therefore the candidate's own, and
the market they meet is random -- so skill at *when to add* is still tested.
What it gives up is novelty: it never combines layers the candidate did not
show. A profile cut short, by the first layer's trim or a session close, is
flattened at the cut.

## What is not blotter, and why

**The clock is the trading clock.** Blotter lays durations out in calendar
time over bars that exist around the clock. Tick data has no prices while the
market is shut, so a calendar-time replicate would open trades on a Saturday at
Friday's close. Every duration here -- observed and replicated -- is measured
over the scored sessions only, concatenated, which is the same layout with the
dead time removed.

**Overnight holding follows the candidate.** A candidate with no round turn
that opens in one session and closes in another never holds overnight, so its
replicates must not either: a replicate trade that would run past its
session's close is closed at the close, as the candidate itself had to be. The
layout is untouched -- the next row still starts where blotter would start it
-- so only that one trade's duration changes. A candidate that held overnight
even once gets replicates that may, on the same trading clock.

**Single asset only.** Blotter runs one simulation per symbol and adds them.
What a multi-asset strategy chooses is mostly *which* instrument, and timing
replicates cannot test that, so a multi-asset blotter is refused rather than
answered with a null for a question it did not ask.

**The statistic is the gate's.** Blotter reports P&L statistics without fees.
Here each replicate becomes a fill log, is reconstructed with the candidate's
commission, and is scored by `metrics.session_returns` and
`scorer.score_returns` -- the idle charge, the exposure, the per-fill P&L
attribution and the Sortino-to-Sharpe fallback included -- because a null that
measures a different statistic from the score is not a null for it.
`replicate_scores` takes the arithmetic shortcut and `tests/test_txnsim.py`
holds it to the slow path draw for draw.

## What has not been measured

Its error rate under `max_of_n`. Until an FWER table exists for it, no
campaign result built on it is calibrated. Per-trial calibration on NQ found
it counts volatility timing as skill when costs are high.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Sequence

from ...types import Blotter, FillRecord, Side
from ..metrics import IDLE_CHARGE, SessionSpan

B_DEFAULT = 10_000
FUDGE_FACTOR = 1.1          # blotter's `fudgefactor`, sizing each sampling batch
MODES = ("layered", "flat.to.flat")
DEFAULT_MODE = "layered"

# A position profile: (offset from the round turn's start, position after) at
# every change, on the trading clock. The first entry is (0, Init.Qty) and the
# last is (duration, 0).
Profile = tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class Period:
    """One row of blotter's `backtest.trades`: a round turn, or a flat period.

    `duration` is on the trading clock, in nanoseconds. `quantity` is the
    signed opening size (`Init.Qty`), and 0 for a flat period. `profile` is the
    round turn's whole position path; empty for a flat period.
    """

    duration: int
    quantity: int
    profile: Profile = ()


@dataclass(frozen=True)
class TradeFacts:
    """The stylized facts a replicate is built from."""

    periods: tuple[Period, ...]
    first_start: int            # trading-clock time of the first opening
    calendar: int               # first opening to last close, as blotter's
    holds_overnight: bool       # any observed round turn spans sessions
    layered: bool               # any observed round turn added to its position
    round_turns: int


class TradingClock:
    """The scored sessions laid end to end, with the time between them removed.

    `clock(ts)` maps an exchange timestamp to trading time and `ts_at(t)` maps
    back. A timestamp in a gap between sessions sits at the end of the session
    before it, which is where a fill stamped there is attributed too.
    """

    def __init__(self, calendar: Sequence[SessionSpan]):
        spans = sorted(calendar, key=lambda s: s.start_ts)
        if not spans:
            raise ValueError("a trading clock needs at least one session")
        self.spans = spans
        self.starts = [s.start_ts for s in spans]
        self.offsets = [0]
        for s in spans:
            self.offsets.append(self.offsets[-1] + (s.end_ts - s.start_ts))
        self.total = self.offsets[-1]

    def index_of(self, ts: int) -> int:
        """The session a timestamp belongs to, gaps going to the one before."""
        return max(0, bisect.bisect_right(self.starts, ts) - 1)

    def clock(self, ts: int) -> int:
        i = self.index_of(ts)
        span = self.spans[i]
        return self.offsets[i] + min(max(ts, span.start_ts), span.end_ts) - span.start_ts

    def session_at(self, t: int, *, closing: bool = False) -> int:
        """The session index trading time `t` falls in.

        An instant exactly on a boundary opens the later session and closes
        the earlier one, so a trade ending at a session's close is not carried
        into the next.
        """
        if closing:
            i = bisect.bisect_left(self.offsets, t) - 1
        else:
            i = bisect.bisect_right(self.offsets, t) - 1
        return min(max(i, 0), len(self.spans) - 1)

    def ts_at(self, t: int, *, closing: bool = False) -> int:
        i = self.session_at(t, closing=closing)
        return self.spans[i].start_ts + (t - self.offsets[i])


def trade_facts(blotter: Blotter, calendar: Sequence[SessionSpan]) -> TradeFacts:
    """`perTradeStats(tradeDef = 'flat.to.flat', includeFlatPeriods = TRUE)`,
    with each round turn's position profile kept alongside `Init.Qty`."""
    rows = sorted(blotter.rows, key=lambda r: (r.seq, r.order_id))
    assets = {r.asset for r in rows}
    if len(assets) > 1:
        raise ValueError(
            f"txnsim is single-asset: this blotter trades {len(assets)} "
            "assets, and what a multi-asset strategy chooses is which "
            "instrument, which random timing cannot test")
    clock = TradingClock(calendar)
    periods: list[Period] = []
    opened = closed = None      # trading-clock times
    opened_ts = None
    qty = 0
    path: list[tuple[int, int]] = []
    overnight = layered = False
    turns = 0
    prev = 0
    for r in rows:
        t = clock.clock(r.ts)
        if prev == 0 and r.position_after != 0:
            opened, opened_ts = t, r.ts
            qty = r.size * r.side.sign
            path = [(0, r.position_after)]
            if closed is not None:
                periods.append(Period(opened - closed, 0))
        elif prev != 0:
            if r.position_after != 0 and abs(r.position_after) > abs(prev) and \
                    (r.position_after > 0) == (prev > 0):
                layered = True
            path.append((t - opened, r.position_after))
            if r.position_after == 0:
                closed = t
                periods.append(Period(closed - opened, qty, tuple(path)))
                if clock.index_of(r.ts) != clock.index_of(opened_ts):
                    overnight = True
                turns += 1
        prev = r.position_after
    if turns == 0:
        return TradeFacts((), 0, 0, False, False, 0)
    first = next(clock.clock(r.ts) for r in rows if r.position_after != 0)
    if prev == 0 and clock.total > closed:
        periods.append(Period(clock.total - closed, 0))       # trailing flat, kept
    # A position still open at the end is not a round turn: blotter's
    # flat.to.flat has no End for it, and the flat period before it was
    # already recorded when it opened.
    return TradeFacts(tuple(periods), first, closed - first, overnight,
                      layered, turns)


# --------------------------------------------------------------------------- #
# sampling
# --------------------------------------------------------------------------- #
def _subsample(periods: Sequence[Period], index: Sequence[int], target: int,
               mean: float, rng) -> list[Period]:
    """Blotter's sampling loop: `idxexpr.wr`, and `subsample` in `tradesample`.

    Draw from `index` with replacement in batches -- `round(len(index) * 1.1)`
    first, then `round(remaining / mean * 1.1)` -- until the summed duration
    reaches `target`; trim the row that crosses it to land exactly, and drop
    every row after it.
    """
    out: list[Period] = []
    total = 0
    nsamples = round(len(index) * FUDGE_FACTOR)
    while total < target:
        idx = rng.integers(0, len(index), size=max(nsamples, 1))
        batch = [periods[index[int(i)]] for i in idx]
        out.extend(batch)
        total += sum(p.duration for p in batch)
        nsamples = round(((target - total) / mean) * FUDGE_FACTOR)
        nsamples = 1 if nsamples == 0 else nsamples
    running = 0
    xs = len(out) - 1
    for k, p in enumerate(out):
        if running + p.duration >= target:
            xs = k
            break
        running += p.duration
    out = out[: xs + 1]
    last = out[-1]
    out[-1] = Period(target - running, last.quantity, last.profile)
    return out


def _sample_with_replacement(periods: Sequence[Period], rng) -> list[Period]:
    """`idxexpr.wr`: every row, trades and flat periods together."""
    target = sum(p.duration for p in periods)
    return _subsample(periods, range(len(periods)), target,
                      target / len(periods), rng)


def _sample_without_replacement(periods: Sequence[Period], rng) -> list[Period]:
    """`idxexpr.nr`: the observed rows in a random order."""
    return [periods[int(i)] for i in rng.permutation(len(periods))]


def _first_layer(facts: TradeFacts, rng) -> list[Period]:
    """`tradesample`'s first layer: long/short/flat ratios preserved."""
    periods = facts.periods
    mean = sum(p.duration for p in periods) / len(periods)
    flat_i = [i for i, p in enumerate(periods) if p.quantity == 0]
    long_i = [i for i, p in enumerate(periods) if p.quantity > 0]
    short_i = [i for i, p in enumerate(periods) if p.quantity < 0]
    dur = lambda ix: sum(periods[i].duration for i in ix)  # noqa: E731
    flat_d, long_d, short_d = dur(flat_i), dur(long_i), dur(short_i)
    flats = _subsample(periods, flat_i, flat_d, mean, rng) if flat_d > 0 else []
    longs = _subsample(periods, long_i, long_d, mean, rng) if long_d > 0 else []
    shorts = _subsample(periods, short_i, short_d, mean, rng) if short_d > 0 else []

    def head(rows: list[Period], bound: float) -> list[Period]:
        """`rows[1:last(which(cumsum(duration) < bound))]`, empty when none."""
        out, running = [], 0
        for p in rows:
            if running + p.duration >= bound:
                break
            running += p.duration
            out.append(p)
        return out

    layer = list(flats)
    ratio = long_d / (long_d + short_d) if long_d + short_d > 0 else 0.0
    layer += head(longs, round((facts.calendar - flat_d) * ratio))
    layer += head(shorts, facts.calendar - sum(p.duration for p in layer))
    return [layer[int(i)] for i in rng.permutation(len(layer))]


# --------------------------------------------------------------------------- #
# layout and pricing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SimTrade:
    """One replicate round turn, on the trading clock.

    `profile` is the position path relative to `start`, already cut at `end`:
    it ends with `(end - start, 0)`.
    """

    start: int
    end: int
    profile: Profile

    @property
    def quantity(self) -> int:
        return self.profile[0][1]


def _cut(profile: Profile, length: int) -> Profile:
    """The profile up to `length`, flattened there if it was still open."""
    kept = [(o, q) for o, q in profile if o < length]
    return tuple(kept) + ((length, 0),)


def layout(rows: Sequence[Period], facts: TradeFacts, clock: TradingClock, *,
           mode: str = DEFAULT_MODE) -> list[SimTrade]:
    """Rows end to end from the first opening, as blotter builds `start`."""
    out: list[SimTrade] = []
    t = facts.first_start
    for p in rows:
        start, end = t, t + p.duration
        t = end
        if p.quantity == 0 or p.duration <= 0:
            continue
        start, end = min(start, clock.total), min(end, clock.total)
        if not facts.holds_overnight:
            i = clock.session_at(start)
            end = min(end, clock.offsets[i + 1])
        if end <= start:
            continue
        profile = p.profile if mode == "layered" and p.profile else \
            ((0, p.quantity), (p.duration, 0))
        out.append(SimTrade(start, end, _cut(profile, end - start)))
    return out


def replicate(facts: TradeFacts, clock: TradingClock, rng, *,
              replacement: bool = True, mode: str = DEFAULT_MODE) -> list[SimTrade]:
    """One random strategy with the candidate's stylized facts."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if not facts.periods:
        return []
    if mode == "layered":
        if not replacement:
            raise ValueError("mode 'layered' samples with replacement, as "
                             "blotter's tradesample does")
        rows = _first_layer(facts, rng)
    elif replacement:
        rows = _sample_with_replacement(facts.periods, rng)
    else:
        rows = _sample_without_replacement(facts.periods, rng)
    return layout(rows, facts, clock, mode=mode)


class PriceTape:
    """`findInterval` over the dataset's own trades, one session at a time.

    The then-current price is the last trade at or before the instant, inside
    the session. Before a session's first trade there is no current price yet,
    and the session's first trade is used rather than the previous session's
    last -- which would quietly be an overnight hold.
    """

    def __init__(self, ts_by_session: Sequence[Sequence[int]],
                 px_by_session: Sequence[Sequence[float]]):
        import numpy as np

        self.ts = [np.asarray(t, dtype=np.int64) for t in ts_by_session]
        self.px = [np.asarray(p, dtype=float) for p in px_by_session]
        for i, t in enumerate(self.ts):
            if t.size == 0:
                raise ValueError(f"session index {i} has no trades to price at")

    def at(self, session: int, ts: int) -> tuple[int, float]:
        import numpy as np

        t = self.ts[session]
        k = int(np.searchsorted(t, ts, side="right")) - 1
        k = max(k, 0)
        return int(t[k]), float(self.px[session][k])


def _priced(trade: SimTrade, clock: TradingClock, tape: PriceTape):
    """Each change in a trade's profile as (session index, ts, price, position)."""
    out = []
    last = len(trade.profile) - 1
    for k, (off, pos) in enumerate(trade.profile):
        closing = k == last
        t = trade.start + off
        si = clock.session_at(t, closing=closing)
        ts, px = tape.at(si, clock.ts_at(t, closing=closing))
        out.append((si, ts, px, pos))
    return out


def replicate_fills(trades: Sequence[SimTrade], clock: TradingClock,
                    tape: PriceTape, calendar: Sequence[SessionSpan]) -> list[FillRecord]:
    """`txnsim.txns`: every change in position, at the then-current price."""
    events = []
    for k, tr in enumerate(trades):
        prev = 0
        for j, (si, ts, px, pos) in enumerate(_priced(tr, clock, tape)):
            if pos != prev:
                events.append((ts, k, j, si, pos - prev, px))
            prev = pos
    # Trades never overlap and a close sits before the next open on the same
    # tick, so (time, trade, step) is the order the positions happened in.
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    out = []
    for seq, (ts, _, _, si, q, px) in enumerate(events):
        out.append(FillRecord(seq=seq, ts=ts, session=clock.spans[si].session,
                              order_id=seq + 1,
                              side=Side.BUY if q > 0 else Side.SELL,
                              size=abs(q), price=px, liquidity="taker",
                              tag="txnsim"))
    return out


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def score_replicate(trades: Sequence[SimTrade], clock: TradingClock,
                    tape: PriceTape, calendar: Sequence[SessionSpan], *,
                    commission_per_contract: float,
                    idle_charge: float = IDLE_CHARGE,
                    notional_base: float = 1.0) -> float:
    """The slow path: fills -> `reconstruct` -> the gate's own statistic."""
    from ...engine.reconstruct import reconstruct
    from ..metrics import session_returns
    from ..scorer import score_returns

    blotter = reconstruct(replicate_fills(trades, clock, tape, calendar),
                          commission_per_contract=commission_per_contract)
    return score_returns(session_returns(
        blotter, calendar, notional_base=notional_base,
        idle_charge=idle_charge)).primary


def replicate_scores(trades: Sequence[SimTrade], clock: TradingClock,
                     tape: PriceTape, calendar: Sequence[SessionSpan], *,
                     commission_per_contract: float,
                     idle_charge: float = IDLE_CHARGE,
                     notional_base: float = 1.0) -> float:
    """`score_replicate` without building a blotter.

    Replicate trades never overlap, so each is its own flat-to-flat book: each
    fill's realised P&L -- by `reconstruct`'s average-price rule -- and its
    commission land in that fill's session, its exposure is its own interval
    clipped per session, and the position scale is the largest position held.
    `tests/test_txnsim.py` holds this to the slow path.
    """
    import math

    from ..scorer import score_returns

    n = len(calendar)
    order = {s.session: i for i, s in enumerate(calendar)}
    pnl: list[list[float]] = [[] for _ in range(n)]
    held = [0.0] * n
    scale = 0
    for tr in trades:
        steps = _priced(tr, clock, tape)
        pos, avg = 0, 0.0
        for si, ts, px, new in steps:
            signed = new - pos
            if signed == 0:
                continue
            realized = 0.0
            if pos != 0 and (pos > 0) != (signed > 0):
                closing = min(abs(signed), abs(pos))
                realized = (px - avg) * closing * (1 if pos > 0 else -1)
                if not ((pos > 0) == (new > 0) or new == 0):
                    avg = px                        # flipped: opened here
            else:
                avg = (avg * abs(pos) + px * abs(signed)) / (abs(pos) + abs(signed))
            pos = new
            scale = max(scale, abs(pos))
            pnl[order[clock.spans[si].session]].append(
                realized - commission_per_contract * abs(signed))
        ots, cts = steps[0][1], steps[-1][1]
        if cts > ots:
            for i in range(steps[0][0], steps[-1][0] + 1):
                span = clock.spans[i]
                a, b = max(ots, span.start_ts), min(cts, span.end_ts)
                if b > a:
                    held[order[span.session]] += float(b - a)
    scale_f = float(scale) if scale > 0 else 1.0
    out = []
    for s, span in enumerate(calendar):
        total = float(span.end_ts - span.start_ts)
        f = min(1.0, held[s] / total) if total > 0 else 0.0
        charge = (-idle_charge * scale_f * (1.0 - f) / notional_base
                  if idle_charge > 0 else 0.0)
        out.append(math.fsum(pnl[s]) / notional_base + charge)
    return score_returns(out).primary


def null_sample(blotter: Blotter, calendar: Sequence[SessionSpan],
                tape: PriceTape, *, commission_per_contract: float,
                b: int = B_DEFAULT, seed: int = 17, replacement: bool = True,
                mode: str = DEFAULT_MODE, idle_charge: float | None = None,
                notional_base: float = 1.0) -> list[float]:
    """`b` scores of random strategies sharing this candidate's style.

    `tape` must price exactly the sessions of `calendar`, in calendar order.
    An empty list for a candidate that never completed a round turn, as the
    sign-flip null returns: there is no style to learn.
    """
    import numpy as np

    charge = IDLE_CHARGE if idle_charge is None else idle_charge
    cal = sorted(calendar, key=lambda s: s.start_ts)
    if len(tape.ts) != len(cal):
        raise ValueError(f"tape has {len(tape.ts)} sessions for a calendar of "
                         f"{len(cal)}; the null would price trades in sessions "
                         "the score never saw")
    facts = trade_facts(blotter, cal)
    if facts.round_turns == 0:
        return []
    clock = TradingClock(cal)
    rng = np.random.default_rng(seed)
    return [replicate_scores(replicate(facts, clock, rng, replacement=replacement,
                                       mode=mode),
                             clock, tape, cal,
                             commission_per_contract=commission_per_contract,
                             idle_charge=charge, notional_base=notional_base)
            for _ in range(b)]
