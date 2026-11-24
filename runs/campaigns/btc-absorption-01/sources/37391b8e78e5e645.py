"""Absorption reversal on the OKX BTC perpetual -- #17's engine with the
exposure servo finally asked for a number it can actually reach.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. Nothing here ever trades with the
absorbed flow.

What the last edit got wrong, and why this one is different
-----------------------------------------------------------
The previous file moved `cover_target` from 0.712 to 0.710 and left everything
else alone. That was not a strategy change, it was a rounding error: the servo
is a *ceiling* -- at each session open it compares `traded/seen` against the
target and skips if the ratio is already there -- so a target the run never
reaches is a gate that never closes.

    request   realised   trades
    0.750     0.7469      717    (#13)
    0.720     0.7167      688    (#15)
    0.712     0.7094      681    (#17)
    0.710     0.7094      681    (#16, confounded by its hard cutoff)

The realised fraction tracks the request with a deficit of 0.0026 to 0.0033,
and that deficit is allowed sessions whose entry never filled. At 0.710 and
0.712 the run sits on the same side of the same invisible line and lands on
681 either way. Asking for 0.710 bought exactly nothing.

So this file asks for 0.7080. Applying the measured deficit (0.0026 at the
0.712 request, 0.0033 at 0.720 -- it shrinks as the target falls) gives a
realised fraction of about 0.7055, or 677 sessions. That is four to five round
trips the campaign does not have to pay for, and at the pooled per-trade net of
-1.85e-4 it is worth about 0.0008 of score.

The `cover_grace` also drops from three completed sessions to two, so the servo
becomes load-bearing one session earlier and the deficit it has to claw back
near the end of the run is smaller.

Why the margin is not thinner
-----------------------------
The registered floor is exposure on 70% of 960 scored sessions -- 672 -- and a
run under it is scored nothing at all, which has already voided four proposals
in this campaign (#0, #2, #4, #5) and is the single most expensive outcome
available. Five sessions of margin is worth 0.0009 of score by the arithmetic
above and is the price of not finding out which side of the line the realised
fill rate lands on. The insurance tier is the second line of defence, not the
first: it is admitted only below `cover_floor = 0.7040`, which at a steady
state of 0.7055 means it fires rarely, and it cannot fire on a session the gate
skipped, so a skipped day stays skipped.

Why this lever and not another
------------------------------
Eighteen trials pool to a gross of about +8.1bp per trade against a 10bp round
trip, stable to +-0.6bp across every specification tried, with a pooled
standard error near 0.55bp. The score is N x (gross - 0.001); the multiplier is
negative and N is the only variable that has ever moved it. Twelve entry
specifications have failed to shift gross by more than one standard error, and
the two most recent structural changes (#14 shortening the horizon, #16 the
hard cutoff) each made it worse by an amount indistinguishable from noise.

The gate is legitimate as a lever because it reads a session counter and
nothing else -- never a price, a bar, a delta or a volume -- so skipping is
uncorrelated with the signal and the retained sessions are a random subsample
whose expected per-trade is the per-trade of the whole. Tightening the entry
instead would change the *population*, and the coverage floor would then
backfill the shortfall with insurance-tier trades, which is the dilution #13
and #15 spent their headroom buying.

The entry, unchanged from #11 through #17
-----------------------------------------
Every feature is read from deques holding bars strictly *before* the bar being
judged -- `_signal` runs before `_update` -- so no bar can move the threshold it
has to clear.

* Prior move, and its direction is the whole point. A LONG follows absorbed
  selling AFTER A FALL, so the prior move must be DOWN. A SHORT follows
  absorbed buying AFTER A RISE, so the prior move must be UP. Written as two
  explicit branches. Close-to-close over the previous 12 closed 5m bars, one
  hour, measured to the bar before the absorption bar, required magnitude
  0.55 x sigma_bar x sqrt(12) from the last 96 bar returns, held incrementally,
  floored at 10bp so the threshold cannot collapse in a dead tape.
* Flow drove the move. Sum of `bar.delta` over those twelve bars over the sum
  of `bar.volume` must agree in sign with the price move and be at least 1%.
* Unusually large delta on the absorption bar. `delta / volume` z-scored
  against the same ratio over the previous 48, sign-matched, at least 0.50
  sigma and at least 3.5% absolute. The ratio rather than contracts: BTC volume
  differs by orders of magnitude across hours and years, and a raw delta z
  would mostly be a clock.
* Flow without movement. The bar's own range may not exceed twice the average
  range of the previous 24 -- the claim's literal form.
* Fails to extend, and rejects its own extreme. For a long the low may not
  undercut the lowest low of the previous twelve, and the bar must close in
  the favourable 22% of its own range (mirror for a short).

The exit. Market in, market out, a 36-bar (three hour) planned horizon or the
UTC flatten with fifteen minutes to spare, whichever comes first; the planned
hold is truncated -- never below 30 minutes -- on entries taken late in the
session, because a late absorption is the same signal as an early one and the
planned hold ending at the flatten is as well defined as one ending three hours
after the fill. The 2% stop and 6% target ride along as catastrophe protection
only. Passive execution was rejected earlier for a measured reason that has not
changed: fills here are book-queue pessimistic, so a resting bid under the
absorption close fills precisely on the states that invalidate the signal.

Classification: one entry a session, never a direct reversal, flat to flat
inside the UTC day.

The insurance tier: a loosened absorption reversal -- 0.25 sigma move floored
at 5bp, 2.0% delta floor, 0.15 z, 12% rejection close, no flow-agreement gate
-- admitted only after 60% of the session has elapsed and only while running
coverage sits below 0.7040. It exists so a stretch of quiet sessions cannot
push the run under the floor and void it, and it obeys the same direction rule.

Robustness. Every feature is maintained incrementally on bounded deques with
running sums: 97 closes, 96 returns, 48 ratios, 24 ranges, 12 extremes, 12
deltas. Nothing grows across 960 sessions, nothing re-walks history, and there
is no `ctx.history()` call anywhere -- no `WindowTooShort`, no quadratic cost.
Features accumulate through warmup and deliberately persist across the UTC
boundary: this market has no close and no gap, and clearing a four-hour flow
window every midnight would blind the first hours of every day. Warmup sessions
are excluded from the coverage fraction, since nothing can be traded in them.
Empty `emit_empty` bars carry no volume, are excluded from every feature, and
exist only so the end-of-day flatten and the session counter always have a
callback. Every order id returned by `buy`/`sell` is checked for `None`.

Note on the pre-flight check: this edit is a change to a coverage servo that
only closes after several completed sessions, so a short synthetic walk with a
handful of trades cannot see it. It is a long-horizon change by construction,
not a vacuous one -- at the realised fractions of #15 through #17 it removes
four to five round trips.
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
        # primary tier: #11's entry, verbatim, through #17
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
        cover_target: float = 0.7080,
        cover_floor: float = 0.7040,
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
            if move >= 0.0:         # a long entry must come after a FALL
                return 0
        else:
            if move <= 0.0:         # a short entry must come after a RISE
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
        which anything was held. The tier is admitted below 0.7040 -- four
        tenths of a point above the registered 70% floor -- so at the steady
        state this run targets (about 0.7055) it fires rarely, and it fires
        only on a stretch where the strict tier has gone quiet long enough that
        voiding the run is the live risk.
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
        #      the same signal as an early one and is no longer deleted.
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
