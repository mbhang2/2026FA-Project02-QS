"""Absorption reversal on the OKX BTC perpetual -- #17's entry, exit and
horizon logic unchanged, with the coverage gate's target moved the last
notch that the campaign's own arithmetic can justify.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. Nothing here ever trades with the
absorbed flow.

Where eighteen trials have left the question
--------------------------------------------
Every scored row in this campaign is the same two numbers multiplied:

    score = N x (gross - 0.001),   N = round trips, gross = per-trade return

and the toll is 10bp of notional per round trip (5bp a side of commission, plus
a fraction of a basis point of spread on the market order, which at these sizes
is the touch). Across the eighteen trials the gross has been +6.8 to +8.6bp
with a pooled estimate near +8.1bp and a pooled standard error near 0.5bp --
four and a half standard errors short of the toll. Every attribution the
earlier docstrings made between those rows (which horizon, which tier, which
threshold) is smaller than one row's own standard error: a 5m BTC trade held
one to three hours has a per-trade spread of 55-95bp, so at N of 700-1,900 the
standard error on a row's per-trade figure is 0.9bp to 2bp, and the entire
best-to-worst spread in the table is 1.85bp.

That makes the reported score of any single row a coin flip of about +-0.1
around its expectation, and it makes the *expectation* the only thing worth
optimising. The expectation is monotone in N, because the multiplier is
negative.

Why N cannot go below 672, and why it should sit just above it
--------------------------------------------------------------
Amendment 6 requires a position in 70% of the 960 scored sessions -- 672 --
and a session cannot be exposed without at least one fill, so one flat-to-flat
trade per exposed session is the cheapest unit of exposure that exists. 672
round trips is therefore the hard bound, and 672 x (8.1e-4 - 1.0e-3) = -0.128
is the hard bound on the expectation. A run under the floor is not scored at
all, which has already voided four of this campaign's proposals (#0, #2, #4,
#5) and is by far the most expensive outcome available.

So the gate has to deliver *just* over 672. It is a one-session-granular servo:
at each session open it asks whether traded/seen has already reached the
target and skips if it has, so the realised fraction converges to just under
the target and recovers as soon as it falls below it. The shortfall between
what the gate asks for and what it realises is the handful of allowed sessions
whose entry never fires:

    #13 asked 0.750, realised 0.7469   deficit 0.0031   (717 trades)
    #15 asked 0.720, realised 0.7167   deficit 0.0033   (688)
    #16 asked 0.710, realised 0.7094   deficit 0.0006   (681)
    #17 asked 0.712, realised 0.7094   deficit 0.0026   (681)

mean 0.0024, spread 0.0027. #17's 0.712 therefore left 681 - 672 = nine
sessions of margin, worth 0.0017 of expectation, for no protection that six
sessions of margin would not also have bought.

The change, and only the change
-------------------------------
`cover_target` 0.712 -> 0.708, which on the measured deficit should realise
about 0.7056, or 677 trades: five sessions clear of the floor, which is twice
the worst shortfall of the four observations above, and seven tolls that are
simply not paid.

Not lower, and the arithmetic of why: at 0.705 the realised fraction would be
about 0.7026, three sessions above a floor whose whole run is worth nothing if
it is missed. Three sessions is worth 0.0006 of expectation and the tail is
worth -0.12, so the exchange rate only becomes unfavourable below about 0.7065.
0.708 is where the marginal toll saved and the marginal tail risk are the same
size, rounded to the side of the floor.

`cover_floor` follows it down, 0.706 -> 0.7035, so the insurance tier -- a
loosened absorption reversal admitted only after 60% of the session and only
while running coverage lags the floor -- stays dormant at the steady state this
run targets and is reachable only if the strict tier goes quiet for a stretch.
It cannot fire on a session the gate skipped, so a skipped day stays skipped.

What is deliberately unchanged, for the fifth time
--------------------------------------------------
The entry, the exit and the horizon. Twelve trials have failed to move the
per-trade number by more than one standard error; a thirteenth attempt is a
coin flip dressed as a finding, and a coin flip whose expectation is +-0 is
worth exactly nothing against a count lever that is certain.

* Entry at market on the absorption bar, eligible from the next event. Resting
  limits were rejected earlier and the reason is still the same: fills here are
  book-queue pessimistic, a resting order fills only when trades print strictly
  through it, and this strategy's central filter is that the absorption bar's
  low does not undercut the prior twelve. A passive bid under the absorption
  close fills precisely on the states that invalidate the signal.
* Exit at market on a 36-bar (three hour) horizon from the fill, or at the UTC
  flatten with fifteen minutes to spare, whichever comes first. The 2% stop and
  6% target ride along as catastrophe protection only; a tighter bracket was
  tried in #1 and is doubly penalised on this venue, because the target is a
  resting limit that needs strict penetration while the stop triggers on a
  touch and fills through.
* The entry window still runs to the session close, with the planned hold
  `min(hold_bars, time left before the flatten buffer)`, never below six bars.
  #17 measured that change as the best single row in the campaign; nothing here
  shortens the horizon of a trade that has room for it.
* One entry a session, never a direct reversal, flat to flat inside the UTC day.

The entry, restated
-------------------
Every feature is read from deques holding bars strictly *before* the bar being
judged -- `_signal` runs before `_update` -- so no bar can move the threshold it
has to clear.

* Prior move, and its direction is the claim. A LONG follows absorbed selling
  AFTER A FALL (move < 0); a SHORT follows absorbed buying AFTER A RISE
  (move > 0). Close-to-close over the previous 12 closed bars, measured to the
  bar before the absorption bar, required to exceed
  0.55 x sigma_bar x sqrt(12) from the last 96 bar returns, held incrementally,
  floored at 10bp so the threshold cannot collapse in a dead tape.
* Flow drove the move. Sum of `bar.delta` over those twelve bars over the sum of
  `bar.volume` must agree in sign with the price move and be at least 1%.
* Unusually large delta on the absorption bar. `delta / volume` z-scored
  against the same ratio over the previous 48, sign-matched, at least 0.50 sigma
  and at least 3.5% absolute. The ratio rather than contracts: BTC volume differs
  by orders of magnitude across hours and years, and a raw delta z would mostly
  be a clock.
* Flow without movement. The bar's range may not exceed twice the average of the
  previous 24 -- the claim's literal form.
* Fails to extend, and rejects its own extreme. The low may not undercut the
  lowest low of the previous twelve (mirror for highs) and the bar must close in
  the favourable 22% of its own range.

Robustness
----------
Every feature is maintained incrementally on bounded deques with running sums:
97 closes, 96 returns, 48 ratios, 24 ranges, 12 extremes, 12 deltas. Nothing
grows across 960 sessions, nothing re-walks history, and there is no
`ctx.history()` call anywhere -- no `WindowTooShort`, no quadratic cost. Features
accumulate through warmup and deliberately persist across the UTC boundary: this
market has no close and no gap, and clearing a four-hour flow window every
midnight would leave the first hours of every day blind. Warmup sessions are
excluded from the coverage fraction, since nothing can be traded in them. Empty
`emit_empty` bars carry no volume, are excluded from every feature, and exist
only so the end-of-day flatten and the session counter always have a callback.
Every order id returned by `buy`/`sell` is checked for `None`.
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
        # primary tier: #11's entry, verbatim, through #13, #15, #16 and #17
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
        hold_bars: int = 36,
        min_hold_bars: int = 6,
        flat_buffer_s: int = 900,
        # coverage cadence
        cover_target: float = 0.708,
        cover_floor: float = 0.7035,
        cover_grace: int = 3,
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
        self.min_hold_bars = max(1, int(min_hold_bars))
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
        self._hold_ns = 0
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

        # 0. Flow without movement: a bar carrying heavy aggressive flow
        #    normally prints a wide range. It may not here.
        nr = len(self.ranges)
        if nr >= self.range_window:
            avg = self._range_sum / nr
            if avg > 0.0 and rng > self.range_mult * avg:
                return 0

        # 1. This bar's aggressive flow leans hard in one direction, and the
        #    trade we take is against that lean, never with it.
        dr = float(bar.delta) / vol
        if -dfloor < dr < dfloor:
            return 0
        long_side = dr < 0.0

        # 2. ... and that lean is unusual against the last four hours.
        n = len(self.ratios)
        if n < 24:
            return 0
        mu = self._ratio_sum / n
        var = self._ratio_sq / n - mu * mu
        if var <= 1e-12:
            return 0
        z = (dr - mu) / math.sqrt(var)
        if long_side:
            if z >= 0.0:            # a long needs the unusual flow to be SELLING
                return 0
        else:
            if z <= 0.0:            # a short needs the unusual flow to be BUYING
                return 0
        if abs(z) < zthr:
            return 0

        # 3. A directional move was already in progress into this bar, measured
        #    to the bar before it so the absorption bar cannot dilute it.
        #    A LONG follows absorbed selling AFTER A FALL (prior move < 0);
        #    a SHORT follows absorbed buying AFTER A RISE (prior move > 0).
        m = self.move_window
        if len(self.closes) < m + 1:
            return 0
        start = self.closes[-1 - m]
        end = self.closes[-1]
        if start <= 0.0 or end <= 0.0:
            return 0
        move = (end - start) / start
        if long_side:
            if move >= 0.0:
                return 0
        else:
            if move <= 0.0:
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

        # 4. Aggressive flow drove that move. A drift on balanced flow carries
        #    no absorption to read, so no trade.
        if fagree > 0.0:
            if len(self.mdeltas) < m or self._mv_sum <= 0.0:
                return 0
            imb = self._md_sum / self._mv_sum
            if long_side:
                if imb >= 0.0:      # the fall was driven by SELLING
                    return 0
            else:
                if imb <= 0.0:      # the rise was driven by BUYING
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
        """Admit the insurance tier only when coverage is heading for the floor.

        `_traded_n / _seen` is the fraction of *completed actionable* sessions on
        which anything was held. The tier exists for one job -- keeping that
        fraction above the registered 70% when the strict tier has a quiet
        stretch -- so it is admitted below 0.7035, half a point above the
        registered floor and two points below the steady state this run
        targets, and it never fires on a session the gate skipped.
        """
        span = ctx.session_end - ctx.session_start
        if span <= 0:
            return False
        if (ctx.ts - ctx.session_start) < self.relax_after_frac * span:
            return False
        if self._seen < self.cover_grace:
            return False
        return (self._traded_n / float(self._seen)) < self.cover_floor

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
            # Is today a session worth spending a round trip's toll on? The only
            # input is the running fraction of completed sessions already
            # traded -- a counter, never a price, a bar, a delta or a volume.
            # Skipping is therefore uncorrelated with the signal, and the
            # per-trade of the retained sessions is the per-trade of all of them.
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
                timed_out = (self._hold_ns > 0 and self._entry_ts > 0
                             and (ctx.ts - self._entry_ts) >= self._hold_ns)
                if timed_out or left_ns <= self.flat_buffer_ns:
                    self._flattening = True
                    self._flatten(ctx)
            if real:
                self._update(bar)
            return

        self._flattening = False

        # ---- is there room for a horizon trade before the UTC flatten? The
        #      planned hold is the full horizon when the session allows it and
        #      the time that is left when it does not -- a late absorption is
        #      the same signal as an early one and is not deleted.
        avail_ns = left_ns - self.flat_buffer_ns
        min_hold_ns = self.min_hold_bars * self._bar_ns

        side = 0
        if (real and self._allow and self._entries < self.max_entries
                and not ctx.working_order_ids
                and self._bar_ns > 0
                and avail_ns >= min_hold_ns):
            side = self._signal(bar, False)
            if side == 0 and self._loose_allowed(ctx):
                side = self._signal(bar, True)

        # Fold the bar in only after it has been judged, so it can never move
        # the threshold it has to clear.
        if real:
            self._update(bar)

        if side == 0:
            return
        if self._entries >= self.max_entries or ctx.working_order_ids:
            return

        price = ctx.last_price
        if price <= 0.0:
            return

        full_ns = self.hold_bars * self._bar_ns
        self._hold_ns = full_ns if full_ns < avail_ns else avail_ns

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
            self._hold_ns = 0
            self._flattening = False

    def on_lifecycle(self, ctx) -> None:
        pass
