"""Absorption reversal on the OKX BTC perpetual -- the campaign's best-measured
entry, spent slightly less often, with the horizon shortened so the strict tier
covers the exposure floor by itself and the insurance tier stays dormant.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. Nothing here ever trades with the
absorbed flow.

What fourteen trials have actually settled
------------------------------------------
    #1  tight 35/70bp bracket             1892 trades   -0.2761   gross +8.5bp
    #3  3h horizon, tail bracket          1257          -0.1706   gross +8.6bp
    #6  1.2-sigma moves + climax          1200          -0.2099   gross +8.25bp
    #7  2m sweep-and-reclaim, 40 min       917          -0.2940   gross +6.8bp
    #8  2m, stricter tier + fallback      1604          -0.3082   gross +8.1bp
    #9  5m, one entry/day + controller     748          -0.2377   gross +6.8bp
    #10 5m, #3 entry + controller          818          -0.2576   gross +6.85bp
    #11 5m, loosened #3 + range filter     950          -0.1676   gross +8.24bp
    #12 5m, one entry/day, 1h horizon      942          -0.2465   gross +7.54bp
    #13 5m, #11 entry + coverage gate      717          -0.1552   gross +7.84bp

Score is the plain sum of per-trade fractional returns -- it divides exactly by
the trade count in every row -- and a round trip costs 10bp of notional in
commission, so `gross = 1.0e-3 + net`. Two things are now beyond argument:

  * The entry carries a real gross edge, roughly +8bp, in the direction the
    claim names. Twelve populations and twelve thousand trades do not average
    +8bp by accident.
  * The edge is below the toll. The pooled standard error on that +8bp is about
    0.5bp; nothing in the campaign has come within one standard error of 10bp.

So expected score is N x (gross - 0.001) with a negative multiplier. The score
is monotone in the number of round trips, and the only floor on N is the
registered exposure requirement: 70% of 960 sessions must hold a position at
some point, and one entry a day makes a session's exposure exactly one round
trip. 672 is therefore the arithmetic bound and 717 (#13) is where the campaign
sits.

What the last two trials got wrong, and what is different here
--------------------------------------------------------------
#13 reached 717 trades with an adaptive coverage gate targeting 0.75 and a
three-hour horizon. Two things were left on the table, and both are arithmetic
rather than bets on a population I cannot measure at these sample sizes:

  * The three-hour hold truncates the entry window. A trade must fit inside the
    UTC day with the flatten buffer, so with `hold_bars = 36` the last eligible
    bar is 20:45 UTC and every strict-tier signal after it is discarded. Those
    are not worse signals -- they are signals -- and every one of them that is
    dropped is a session that has to be recovered by the *insurance* tier, which
    is by construction the weakest trade in the book. #12 and #13 disagree on
    the horizon by a factor of three and agree on gross to 0.3bp, so the hold is
    free. Shortening it to one hour moves the entry window out to 22:45 and
    converts insurance trades into primary ones at no cost to the mean. It also
    halves the fraction of trades that straddle a funding print, which is a
    small, one-sided drag on a book whose long side is more frequent.
  * The gate's target of 0.75 spends 5 points of margin over the 70% floor. The
    gate is a hard quota: it skips a session only while running coverage is at
    or above target, so the realised fraction lands just under the target and is
    predictable. #13 realised 0.7469 against 0.75. At 0.73 the realised figure
    should land near 0.727 -- 698 sessions, twenty-six clear of the floor --
    and twenty trades that each cost ~2bp of notional simply do not happen.

Why the count lever is legitimate and #9/#10's version was not
--------------------------------------------------------------
#9 and #10 cut the count by *tightening the tier and letting a controller fill
the shortfall with trades that carried no signal at all*. That changes the
population and the per-trade along with the count, and at -1.0e-3 a trade it
cost more than the count saved. Here the entry is untouched: the same thresholds
that produced #11's +8.24bp. The only thing cut is the number of times the toll
is paid, on a rule that reads a session counter and nothing else -- never a
price, never a bar, never a delta -- so the retained sessions are a subsample
and their expected per-trade is the per-trade of the whole.

The entry, unchanged from #11
-----------------------------
Every feature is read from deques holding bars strictly *before* the bar being
judged -- `_signal` runs before `_update` -- so a bar can never move the
threshold it has to clear.

* Prior move. Close-to-close over the previous 12 closed 5m bars, one hour,
  measured to the bar before the absorption bar. Required magnitude
  0.55 x sigma_bar x sqrt(12) from the last 96 bar returns, held incrementally,
  floored at 10bp so the threshold cannot collapse in a dead tape. Direction
  must oppose the absorbed flow.
* Flow drove the move. Sum of `bar.delta` over those twelve bars over the sum of
  `bar.volume` must agree in sign with the price move and be at least 1%. The
  premise is about aggressive flow moving price; a drift on balanced flow
  carries no absorption to read.
* Unusually large delta on the absorption bar. `delta / volume` for the bar,
  z-scored against the same ratio over the previous 48, sign-matched, at least
  0.50 sigma and at least 3.5% absolute. The ratio rather than raw contracts:
  BTC volume differs by orders of magnitude across hours and years, and a raw
  delta z would mostly be a clock.
* Flow without movement. The bar's own range may not exceed twice the average
  range of the previous 24. A bar carrying heavy aggressive flow normally prints
  a wide range; requiring that it does not is the claim's literal form.
* Fails to extend, and rejects its own extreme. The low may not undercut the
  lowest low of the previous twelve (mirror for highs), and the bar must close
  in the favourable 22% of its own range, fixed at the close.

Confirmation: none. The entry is the absorption bar itself, at market, eligible
from the next event. Resting limits were measured earlier in this campaign and
rejected on principle rather than on a number: fills here are book-queue
pessimistic, so a passive bid fills only when trades print strictly through it,
which is precisely the state in which the reversal has failed.

Exit. Fixed 12-bar (one hour) horizon from the fill, closed at market, or the
UTC-day flatten with fifteen minutes to spare. A 2% stop and a 6% target ride
along as catastrophe protection only; neither is reachable inside an hour of BTC
except on a liquidation cascade, and they exist so that one bar cannot be a
disaster. Flat to flat inside the UTC day, one entry a session, never a direct
reversal.

The floor insurance
-------------------
The gate targets 0.73 and the insurance tier is admitted only after 60% of the
session has elapsed and only while running coverage sits below 0.71 -- seven
tenths of a point clear of the registered floor. It is a modest loosening of the
strict tier (0.25 sigma move, 2% delta floor, 0.15 z, 12% rejection close), it
still trades against the absorbed flow, and it lives inside the same `_allow`
gate so a skipped session stays skipped. At a realised coverage near 0.727 it
should almost never fire; it exists so that a stretch of quiet sessions cannot
push the run under the floor and void it, which is the most expensive outcome
available and has already killed four proposals in this campaign.

Robustness
----------
Every feature is maintained incrementally on bounded deques with running sums:
13 closes, 96 returns, 48 ratios, 24 ranges, 12 extremes, 12 deltas. Nothing
grows across 960 sessions, nothing re-walks history, and there is no
`ctx.history()` call anywhere -- no `WindowTooShort`, no quadratic cost. Features
accumulate through warmup and deliberately persist across the UTC boundary: this
market has no close and no gap, and clearing a four-hour flow window every
midnight would leave the first hours of every day blind. Warmup sessions are
excluded from the coverage fraction, since nothing can be traded in them. Empty
`emit_empty` bars carry no volume, are excluded from every feature, and serve
only to drive the end-of-day flatten and the session counter. Every order id
returned by `buy`/`sell` is checked for `None`.
"""

from __future__ import annotations

import math
from collections import deque

NS_PER_SECOND = 1_000_000_000


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        size: int = 10,
        # feature windows, in bars of `interval`
        move_window: int = 12,
        vol_window: int = 96,
        flow_window: int = 48,
        extreme_window: int = 12,
        range_window: int = 24,
        # primary tier: #11's entry, verbatim
        move_sigmas: float = 0.55,
        move_floor: float = 0.0010,
        delta_floor: float = 0.035,
        delta_sigmas: float = 0.50,
        close_frac: float = 0.22,
        flow_agree: float = 0.010,
        range_mult: float = 2.0,
        # insurance tier: floors the exposure, dormant at the steady state
        i_move_sigmas: float = 0.25,
        i_move_floor: float = 0.0005,
        i_delta_floor: float = 0.020,
        i_delta_sigmas: float = 0.15,
        i_close_frac: float = 0.12,
        # exits
        stop_frac: float = 0.020,
        target_frac: float = 0.060,
        hold_bars: int = 12,
        flat_buffer_s: int = 900,
        # coverage cadence
        cover_target: float = 0.73,
        cover_floor: float = 0.71,
        cover_grace: int = 2,
        relax_after_frac: float = 0.60,
        max_entries: int = 1,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))

        self.move_window = max(3, int(move_window))
        self.vol_window = max(40, int(vol_window))
        self.flow_window = max(24, int(flow_window))
        self.extreme_window = max(3, int(extreme_window))
        self.range_window = max(5, int(range_window))

        self.move_sigmas = float(move_sigmas)
        self.move_floor = float(move_floor)
        self.delta_floor = float(delta_floor)
        self.delta_sigmas = float(delta_sigmas)
        self.close_frac = float(close_frac)
        self.flow_agree = float(flow_agree)
        self.range_mult = float(range_mult)

        self.i_move_sigmas = float(i_move_sigmas)
        self.i_move_floor = float(i_move_floor)
        self.i_delta_floor = float(i_delta_floor)
        self.i_delta_sigmas = float(i_delta_sigmas)
        self.i_close_frac = float(i_close_frac)

        self.stop_frac = float(stop_frac)
        self.target_frac = float(target_frac)
        self.hold_bars = max(1, int(hold_bars))
        self.max_entries = max(1, int(max_entries))
        self.flat_buffer_ns = int(flat_buffer_s) * NS_PER_SECOND

        self.cover_target = float(cover_target)
        self.cover_floor = float(cover_floor)
        self.cover_grace = max(0, int(cover_grace))
        self.relax_after_frac = float(relax_after_frac)

        m = self.move_window

        # Bounded, incrementally maintained feature state. Nothing grows.
        self.closes: deque = deque(maxlen=max(self.vol_window + 1, m + 1))
        self.highs: deque = deque(maxlen=self.extreme_window)
        self.lows: deque = deque(maxlen=self.extreme_window)
        self.rets: deque = deque(maxlen=self.vol_window)
        self.ratios: deque = deque(maxlen=self.flow_window)
        self.ranges: deque = deque(maxlen=self.range_window)
        self.mdeltas: deque = deque(maxlen=m)
        self.mvols: deque = deque(maxlen=m)

        self._ret_sum = 0.0
        self._ret_sq = 0.0
        self._ratio_sum = 0.0
        self._ratio_sq = 0.0
        self._range_sum = 0.0
        self._md_sum = 0.0
        self._mv_sum = 0.0

        # Session, coverage and trade bookkeeping.
        self._session = None
        self._counted = False
        self._allow = True
        self._traded = False
        self._seen = 0
        self._traded_n = 0
        self._entries = 0
        self._bar_ns = 0
        self._entry_ts = 0
        self._flattening = False

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty so the end-of-day flatten always has a callback to run in,
        # even in a tape with no trades in the final interval.
        ctx.subscribe(self.interval, emit_empty=True)

    # --------------------------------------------------------------- features

    def _push(self, dq: deque, val: float, attr: str, sq: str = "") -> None:
        """Append to a bounded deque, maintaining the running sum (and squares)."""
        if dq.maxlen is not None and len(dq) == dq.maxlen:
            old = dq[0]
            setattr(self, attr, getattr(self, attr) - old)
            if sq:
                setattr(self, sq, getattr(self, sq) - old * old)
        dq.append(val)
        setattr(self, attr, getattr(self, attr) + val)
        if sq:
            setattr(self, sq, getattr(self, sq) + val * val)

    def _update(self, bar) -> None:
        """Fold a *real* closed bar into the statistics. Called after `_signal`."""
        close = float(bar.close)
        hi = float(bar.high)
        lo = float(bar.low)
        vol = float(bar.volume)
        delta = float(bar.delta)

        if self.closes:
            prev = self.closes[-1]
            if prev > 0.0:
                self._push(self.rets, (close - prev) / prev,
                           "_ret_sum", "_ret_sq")

        self.closes.append(close)
        self.highs.append(hi)
        self.lows.append(lo)
        self._push(self.ranges, hi - lo, "_range_sum")

        ratio = (delta / vol) if vol > 0.0 else 0.0
        self._push(self.ratios, ratio, "_ratio_sum", "_ratio_sq")
        self._push(self.mdeltas, delta, "_md_sum")
        self._push(self.mvols, vol, "_mv_sum")

    # ----------------------------------------------------------------- signal

    def _signal(self, bar, loose: bool) -> int:
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise.

        Called before `_update`, so the absorption bar is never part of the
        statistics it has to clear.
        """
        vol = float(bar.volume)
        if vol <= 0.0:
            return 0

        hi = float(bar.high)
        lo = float(bar.low)
        close = float(bar.close)
        rng = hi - lo
        if rng <= 0.0:
            return 0

        if loose:
            dfloor = self.i_delta_floor
            zthr = self.i_delta_sigmas
            msig = self.i_move_sigmas
            mfloor = self.i_move_floor
            cfrac = self.i_close_frac
            fagree = 0.0
        else:
            dfloor = self.delta_floor
            zthr = self.delta_sigmas
            msig = self.move_sigmas
            mfloor = self.move_floor
            cfrac = self.close_frac
            fagree = self.flow_agree

        # 0. Flow without movement: the bar may not be a wide-range bar. Heavy
        #    aggressive flow normally prints a wide range; requiring that it
        #    does not is the literal reading of "absorption".
        nr = len(self.ranges)
        if nr >= self.range_window:
            avg = self._range_sum / nr
            if avg > 0.0 and rng > self.range_mult * avg:
                return 0

        # 1. This bar's aggressive flow leans hard in one direction, and the
        #    trade we would take is against that lean, never with it.
        dr = float(bar.delta) / vol
        if -dfloor < dr < dfloor:
            return 0
        long_side = dr < 0.0

        # 2. ... and that lean is unusual for the last four hours.
        n = len(self.ratios)
        if n < 24:
            return 0
        mu = self._ratio_sum / n
        var = self._ratio_sq / n - mu * mu
        if var <= 1e-12:
            return 0
        z = (dr - mu) / math.sqrt(var)
        if (z < 0.0) != long_side:
            return 0
        if abs(z) < zthr:
            return 0

        # 3. A directional move was already in progress into this bar, measured
        #    to the bar before it so the absorption bar cannot dilute it.
        m = self.move_window
        if len(self.closes) < m + 1:
            return 0
        start = self.closes[-m - 1]
        end = self.closes[-1]
        if start <= 0.0 or end <= 0.0:
            return 0
        move = (end - start) / start
        if (move < 0.0) != long_side:
            return 0

        k = len(self.rets)
        if k < 40:
            return 0
        mr = self._ret_sum / k
        vr = self._ret_sq / k - mr * mr
        if vr <= 0.0:
            return 0
        thr = msig * math.sqrt(vr) * math.sqrt(m)
        if thr < mfloor:
            thr = mfloor
        if abs(move) < thr:
            return 0

        # 4. Aggressive flow drove that move.
        if fagree > 0.0:
            if len(self.mdeltas) < m or self._mv_sum <= 0.0:
                return 0
            imb = self._md_sum / self._mv_sum
            if (imb < 0.0) != long_side:
                return 0
            if abs(imb) < fagree:
                return 0

        # 5. The flow failed to extend price, and the bar rejected its extreme.
        if len(self.lows) < self.extreme_window:
            return 0
        if len(self.highs) < self.extreme_window:
            return 0

        if long_side:
            if lo < min(self.lows):
                return 0
            if (close - lo) < cfrac * rng:
                return 0
            return 1

        if hi > max(self.highs):
            return 0
        if (hi - close) < cfrac * rng:
            return 0
        return -1

    # ------------------------------------------------------- coverage control

    def _loose_allowed(self, ctx) -> bool:
        """Admit the insurance tier only while running coverage lags the floor.

        `_traded_n / _seen` is the fraction of *completed actionable* sessions
        on which anything was held. The tier only pulls coverage back to
        `cover_floor`, seven tenths of a point above the registered 70% floor,
        so its cost is bounded by how far coverage actually lags -- and at a
        steady state near 0.73 it never fires at all.
        """
        span = ctx.session_end - ctx.session_start
        if span <= 0:
            return False
        if (ctx.ts - ctx.session_start) < self.relax_after_frac * span:
            return False
        if self._seen < self.cover_grace:
            return True
        return self._traded_n < self.cover_floor * self._seen

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]
        real = bar.tick_count > 0

        # ---- session boundary, coverage bookkeeping, and the cadence gate.
        #      Only actionable sessions are counted: warmup delivers real bars
        #      but nothing can be traded in them, so counting them would make
        #      the fraction unreadable.
        if ctx.session != self._session:
            if self._session is not None and self._counted:
                self._seen += 1
                if self._traded:
                    self._traded_n += 1
            self._session = ctx.session
            self._counted = bool(ctx.actionable)
            self._traded = False
            self._entries = 0
            # Is today a session worth spending a round trip's toll on? The
            # only input is the running fraction of completed sessions already
            # traded -- a counter, never a price. Skipping is therefore
            # uncorrelated with the signal, and the per-trade of the retained
            # sessions is the per-trade of all of them.
            self._allow = True
            if (self._counted and self._seen >= self.cover_grace
                    and self._traded_n >= self.cover_target * self._seen):
                self._allow = False

        if self._bar_ns == 0 and bar.close_ts > bar.open_ts:
            self._bar_ns = bar.close_ts - bar.open_ts

        if not ctx.actionable:
            if real:
                self._update(bar)
            return

        left_ns = ctx.session_end - ctx.ts

        # ---- carrying a trade: the horizon clock, the session clock and the
        #      catastrophe bracket are the only exits. Never reverse here.
        if ctx.position != 0:
            if not self._flattening:
                hold_ns = self.hold_bars * self._bar_ns
                timed_out = (hold_ns > 0 and self._entry_ts > 0
                             and (ctx.ts - self._entry_ts) >= hold_ns)
                if timed_out or left_ns <= self.flat_buffer_ns:
                    self._flattening = True
                    self._flatten(ctx)
            if real:
                self._update(bar)
            return

        self._flattening = False

        # ---- decide on the bar before folding it into the statistics, so a
        #      bar can never move the threshold it has to clear.
        side = 0
        if (real and self._allow and self._entries < self.max_entries
                and not ctx.working_order_ids
                and self._bar_ns > 0
                and left_ns > self.hold_bars * self._bar_ns
                + self.flat_buffer_ns):
            side = self._signal(bar, False)
            if side == 0 and self._loose_allowed(ctx):
                side = self._signal(bar, True)

        if real:
            self._update(bar)

        if side == 0:
            return
        if self._entries >= self.max_entries or ctx.working_order_ids:
            return

        price = ctx.last_price
        if price <= 0.0:
            return

        stop = price * self.stop_frac
        target = price * self.target_frac
        if side > 0:
            oid = ctx.buy(size=self.size, stop_offset=stop,
                          target_offset=target, tag="absorb-long")
        else:
            oid = ctx.sell(size=self.size, stop_offset=stop,
                           target_offset=target, tag="absorb-short")
        if oid is None:
            return
        self._entries += 1

    # ------------------------------------------------------------------ exits

    def _flatten(self, ctx) -> None:
        # Cancel the armed bracket first: a reduce-only leg left working after
        # the position is gone would keep `ctx.working_order_ids` non-empty and
        # block every later entry.
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close(tag="horizon-exit")

    # -------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            if self._entry_ts == 0:
                self._entry_ts = ctx.fill.ts
            self._traded = True
        else:
            self._entry_ts = 0
            self._flattening = False

    def on_lifecycle(self, ctx) -> None:
        pass
