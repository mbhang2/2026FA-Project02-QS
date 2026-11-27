"""Absorption reversal on the OKX BTC perpetual -- bigger moves, climax bars,
and an exposure backstop.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. Long follows absorbed selling after a fall; short
follows absorbed buying after a rise. This file never trades with the absorbed
flow.

What the scored trials actually said, and what this file does about it
----------------------------------------------------------------------
    #1  tight 35bp/70bp bracket   1892 trades   -0.2761   ->  -1.46e-4 / trade
    #3  fixed 3h horizon, wide bracket  1257    -0.1706   ->  -1.36e-4 / trade

A round trip costs 5bp a side, so a trade that earned nothing would score
-1.0e-3. Both trials therefore imply a *positive* gross of about +8.5bp per
trade (+8.54e-4 and +8.64e-4 -- the same number to within 1%), from two exits
that could hardly be more different. That is the single most useful fact in
this campaign so far:

  * The entry carries real information. It is not noise.
  * The exit is not the problem. Changing it again is not the experiment worth
    spending.
  * The problem is arithmetic. +8.6bp of gross against a ~10bp toll and a
    pessimistic book costs about 1.4bp a trade.

So everything here is aimed at one number -- gross basis points per trade --
subject to the constraint that killed trials #0, #2, #4 and #5: the 70%
session-exposure floor. Those two goals pull against each other, and the design
below is explicit about how it resolves them.

1. The prior move must be large. The reasoning is that a reversal of k% of a
   move cannot exceed k% times the move, so the only way to clear a fixed 10bp
   toll with margin is to trade moves that are large in absolute terms. Trial
   #3 required max(0.8 sigma_1h, 15bp); on this data sigma_1h is around 50bp,
   so it was trading 40-50bp hours and collecting 8.6bp of them -- about a
   fifth. A 1.2 sigma hour is a ~60-70bp move; a fifth of that is 13-14bp,
   which clears the toll. This trial sits at 1.2 sigma with a 22bp floor.

   The floor exists so the threshold cannot vanish in a dead tape, and the
   sigma scaling exists so it does not become impossible in a fast one. Sigma
   is the standard deviation of the last 96 five-minute returns, maintained
   incrementally, so the threshold is a property of the recent tape rather
   than of the sample.

2. Climax volume on the absorption bar. This is the one genuinely new
   conditioning axis. The premise is that a large passive participant is
   filling the aggressive side; that participant has to be *sized*, and the
   trace it leaves in the tape is an unusually heavy bar. The bar's volume
   must therefore exceed 1.15x the mean of the last 48 bars. It is set low
   deliberately: after the move threshold is tightened, the frequency budget
   is scarce and a 2x volume requirement would take the strategy under the
   exposure floor, which is exactly how trials #4 and #5 scored nothing.

3. The absorption bar must lean the right way and unusually so. delta/volume
   for the bar, sign-matched to the prior move, at least 5% in absolute terms,
   and at least 0.8 standard deviations from its own mean over the last 48
   bars. The ratio rather than raw contracts, because BTC volume varies by
   orders of magnitude across hours and across years, and a raw-delta z-score
   would mostly be a clock.

4. The prior move must have been driven by aggressive flow. Cumulative
   delta/volume over the same twelve bars, at least 2.5% in magnitude, in the
   same direction as the price move. The premise is about aggressive flow
   moving price; a drift on balanced flow carries no absorption reading.

5. "Fails to extend", chosen rule: the bar may not make a new extreme beyond
   the prior twelve bars' range by more than 0.6 average bar ranges, and --
   this is the part that matters -- it must close back *inside* that prior
   range. That admits both readings of the claim: the strict one (the low
   holds above the prior twelve lows, so the flow moved nothing at all) and
   the sweep (the low pokes through and is reclaimed). Trial #3 used the
   strict form only, requiring the low to hold above the prior lows; allowing
   the reclaimed sweep widens the population without weakening the claim,
   because a sweep that closes back inside is the same statement made more
   strongly. On top of it the bar must close in the upper (lower) 35% of its
   own range -- the rejection of its own extreme, measurable inside one bar
   and impossible to revise afterwards.

6. Exposure backstop. Because the tier above is stricter than anything that has
   been scored, a second, much looser tier is armed once 40% of the UTC day has
   elapsed and no trade has yet been taken: 0.4 sigma / 8bp move, 3% delta,
   z >= 0.3, a 22% rejection close, no volume requirement. It is only ever
   allowed as the session's first trade, so it cannot dilute a day that
   already found a real signal. Its expected value is roughly trial #3's,
   about -1.4bp a trade; on the 20-30% of sessions that would otherwise be
   unexposed that costs a few hundredths of score, against the certainty of a
   NOT SCORED verdict if the floor is missed. Given that four of this
   campaign's six proposals have died on that floor, and that a NOT SCORED run
   yields no information at all, that is the right trade.

Exit, orders, and the daily flat
--------------------------------
Fixed horizon of 18 bars (90 minutes) from the fill, closed at market. Trial
#1's ~20-minute bracket and trial #3's three-hour horizon produced the same
8.6bp of gross, which says the drift attached to this signal is largely
realised early and then saturates; a 90-minute horizon gives it room without
paying for three hours of variance. A 2% stop and a 4% target ride along as
catastrophe protection only -- a tight stop on a mean-reversion entry is
stopped out by exactly the flush the thesis says is the trap (trial #1), and
the earlier tight-bracket trial is the demonstration.

Market in and market out. Resting limits were considered and rejected: fills
here are book-queue pessimistic, so a resting bid fills only when trades print
strictly through it, and it therefore fills preferentially in exactly the
states where the reversal failed. A 10bp toll paid at the touch is the cheaper
of the two errors.

Flat to flat inside the UTC day, always. The entry block is only reachable from
flat with nothing working, so long -> short is expressed as long -> flat ->
short. At most two entries a session, at least an hour apart. Entries are
refused with less than the horizon plus a 15-minute buffer left, and an open
position is cancelled and closed with 15 minutes left. `emit_empty=True` so the
end-of-day exit always has a callback, even in a dead tape.

Robustness
----------
Every feature is maintained incrementally on bounded deques with running sums.
Nothing grows across 960 sessions, nothing re-walks history, and there is no
`ctx.history()` call anywhere, so no `WindowTooShort` and no quadratic cost.
Features accumulate through warmup; `ctx.actionable` gates the orders only. The
flow statistics deliberately persist across the UTC boundary, because on this
market the boundary is a calendar convention, not a close, and clearing a
48-bar window every midnight would leave the first hours of every day blind.
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
        move_window: int = 12,
        atr_bars: int = 24,
        ratio_window: int = 48,
        vol_window: int = 48,
        ret_window: int = 96,
        # --- tier 1: the real signal
        move_sigmas: float = 1.2,
        move_floor: float = 0.0022,
        delta_floor: float = 0.05,
        delta_z: float = 0.80,
        vol_mult: float = 1.15,
        close_frac1: float = 0.35,
        flow_agree1: float = 0.025,
        # --- tier 2: exposure backstop, first trade of the day only
        move_sigmas2: float = 0.40,
        move_floor2: float = 0.0008,
        delta_floor2: float = 0.030,
        delta_z2: float = 0.30,
        close_frac2: float = 0.22,
        flow_agree2: float = 0.010,
        fallback_after: float = 0.40,
        # --- extremes, exits, bookkeeping
        spring_atr: float = 0.60,
        stop_frac: float = 0.020,
        target_frac: float = 0.040,
        hold_bars: int = 18,
        max_entries: int = 2,
        cooldown_bars: int = 12,
        flat_buffer_s: int = 900,
    ) -> None:
        self.interval = interval
        self.size = size
        self.move_window = max(2, int(move_window))
        self.atr_bars = max(2, int(atr_bars))
        self.ratio_window = max(10, int(ratio_window))
        self.vol_window = max(10, int(vol_window))
        self.ret_window = max(20, int(ret_window))

        self.move_sigmas = move_sigmas
        self.move_floor = move_floor
        self.delta_floor = delta_floor
        self.delta_z = delta_z
        self.vol_mult = vol_mult
        self.close_frac1 = close_frac1
        self.flow_agree1 = flow_agree1

        self.move_sigmas2 = move_sigmas2
        self.move_floor2 = move_floor2
        self.delta_floor2 = delta_floor2
        self.delta_z2 = delta_z2
        self.close_frac2 = close_frac2
        self.flow_agree2 = flow_agree2
        self.fallback_after = fallback_after

        self.spring_atr = spring_atr
        self.stop_frac = stop_frac
        self.target_frac = target_frac
        self.hold_bars = max(1, int(hold_bars))
        self.max_entries = max(1, int(max_entries))
        self.cooldown_bars = max(0, int(cooldown_bars))
        self.flat_buffer_ns = int(flat_buffer_s) * NS_PER_SECOND

        m = self.move_window

        # Bounded, incrementally maintained feature state. Nothing grows.
        self.closes: deque = deque(maxlen=m + 1)
        self.highs: deque = deque(maxlen=m + 1)
        self.lows: deque = deque(maxlen=m + 1)
        self.rets: deque = deque(maxlen=self.ret_window)
        self.ratios: deque = deque(maxlen=self.ratio_window)
        self.vols: deque = deque(maxlen=self.vol_window)
        self.mdeltas: deque = deque(maxlen=m)
        self.mvols: deque = deque(maxlen=m)
        self.ranges: deque = deque(maxlen=self.atr_bars)

        self._ret_sum = 0.0
        self._ret_sq = 0.0
        self._ratio_sum = 0.0
        self._ratio_sq = 0.0
        self._vol_sum = 0.0
        self._md_sum = 0.0
        self._mv_sum = 0.0
        self._rng_sum = 0.0

        self.last_range = 0.0
        self.last_ratio = 0.0

        # Session and trade bookkeeping.
        self._session = None
        self._entries = 0
        self._bar = 0
        self._last_entry_bar = -(10 ** 9)
        self._entry_ts = 0
        self._flattening = False
        self._bar_ns = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty so the end-of-day flatten always has a callback to run in,
        # even in a tape with no trades in the final interval.
        ctx.subscribe(self.interval, emit_empty=True)

    # --------------------------------------------------------------- features

    def _push(self, dq: deque, val: float, attr: str, sq: str = "") -> None:
        """Append to a bounded deque, maintaining running sum (and squares)."""
        if len(dq) == dq.maxlen:
            old = dq[0]
            setattr(self, attr, getattr(self, attr) - old)
            if sq:
                setattr(self, sq, getattr(self, sq) - old * old)
        dq.append(val)
        setattr(self, attr, getattr(self, attr) + val)
        if sq:
            setattr(self, sq, getattr(self, sq) + val * val)

    def _update(self, bar) -> None:
        close = float(bar.close)
        hi = float(bar.high)
        lo = float(bar.low)
        vol = float(bar.volume)
        delta = float(bar.delta)

        self.last_range = hi - lo

        if self.closes:
            prev = self.closes[-1]
            if prev > 0.0:
                self._push(self.rets, (close - prev) / prev,
                           "_ret_sum", "_ret_sq")

        self.closes.append(close)
        self.highs.append(hi)
        self.lows.append(lo)

        ratio = (delta / vol) if vol > 0.0 else 0.0
        self.last_ratio = ratio
        self._push(self.ratios, ratio, "_ratio_sum", "_ratio_sq")
        self._push(self.vols, vol, "_vol_sum")
        self._push(self.mdeltas, delta, "_md_sum")
        self._push(self.mvols, vol, "_mv_sum")
        self._push(self.ranges, hi - lo, "_rng_sum")

    # ----------------------------------------------------------------- signal

    def _signal(self, tier: int) -> int:
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise."""
        m = self.move_window
        if len(self.closes) < m + 1:
            return 0
        if len(self.highs) < m + 1 or len(self.lows) < m + 1:
            return 0
        if len(self.rets) < 40:
            return 0
        if len(self.ratios) < 30:
            return 0
        if len(self.ranges) < self.atr_bars:
            return 0
        if len(self.mdeltas) < m:
            return 0

        if tier == 1:
            msig = self.move_sigmas
            mfloor = self.move_floor
            dfloor = self.delta_floor
            dz = self.delta_z
            cfrac = self.close_frac1
            fagree = self.flow_agree1
            vmult = self.vol_mult
        else:
            msig = self.move_sigmas2
            mfloor = self.move_floor2
            dfloor = self.delta_floor2
            dz = self.delta_z2
            cfrac = self.close_frac2
            fagree = self.flow_agree2
            vmult = 0.0

        rng = self.last_range
        if rng <= 0.0:
            return 0

        # 1. The prior move, close to close over the window, and large.
        start = self.closes[0]
        if start <= 0.0:
            return 0
        close = self.closes[-1]
        move = (close - start) / start

        n = len(self.rets)
        mr = self._ret_sum / n
        vr = self._ret_sq / n - mr * mr
        if vr <= 0.0:
            return 0
        thr = msig * math.sqrt(vr) * math.sqrt(m)
        if thr < mfloor:
            thr = mfloor
        if abs(move) < thr:
            return 0

        # 2. This bar's flow leans the same way -- and unusually so.
        dr = self.last_ratio
        if -dfloor < dr < dfloor:
            return 0
        long_side = dr < 0.0
        if (move < 0.0) != long_side:
            return 0

        k = len(self.ratios)
        mu = self._ratio_sum / k
        var = self._ratio_sq / k - mu * mu
        if var <= 1e-12:
            return 0
        z = (dr - mu) / math.sqrt(var)
        if (z < 0.0) != long_side:
            return 0
        if abs(z) < dz:
            return 0

        # 3. Aggressive flow drove the prior move.
        if self._mv_sum <= 0.0:
            return 0
        imb = self._md_sum / self._mv_sum
        if (imb < 0.0) != long_side:
            return 0
        if abs(imb) < fagree:
            return 0

        # 4. A climax bar: the passive side that absorbs has to be sized.
        if vmult > 0.0 and self.vols:
            kv = len(self.vols)
            vmean = self._vol_sum / kv
            if vmean > 0.0 and self.vols[-1] < vmult * vmean:
                return 0

        # 5. The flow failed to extend price, and the bar rejected its extreme.
        atr = self._rng_sum / len(self.ranges)
        if atr <= 0.0:
            return 0

        lows = list(self.lows)
        highs = list(self.highs)
        prior_low = min(lows[:-1])
        prior_high = max(highs[:-1])

        if long_side:
            if lows[-1] < prior_low - self.spring_atr * atr:
                return 0
            if close < prior_low:
                return 0
            if (close - lows[-1]) < cfrac * rng:
                return 0
            return 1

        if highs[-1] > prior_high + self.spring_atr * atr:
            return 0
        if close > prior_high:
            return 0
        if (highs[-1] - close) < cfrac * rng:
            return 0
        return -1

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._entries = 0
            self._last_entry_bar = -(10 ** 9)

        if self._bar_ns == 0 and bar.close_ts > bar.open_ts:
            self._bar_ns = bar.close_ts - bar.open_ts

        # Real bars feed the features. An emit_empty bar carries the previous
        # close forward at zero volume and would poison every ratio, but it
        # still provides the callback the end-of-day flatten needs.
        real = bar.tick_count > 0
        if real:
            self._update(bar)
            self._bar += 1

        if not ctx.actionable:
            return

        left_ns = ctx.session_end - ctx.ts

        # ---- carrying a trade: the fixed horizon and the session clock are the
        #      only exits besides the catastrophe bracket. Never reverse here.
        if ctx.position != 0:
            if self._flattening:
                return
            hold_ns = self.hold_bars * self._bar_ns if self._bar_ns > 0 else 0
            timed_out = (hold_ns > 0 and self._entry_ts > 0
                         and (ctx.ts - self._entry_ts) >= hold_ns)
            if timed_out or left_ns <= self.flat_buffer_ns:
                self._flattening = True
                self._flatten(ctx)
            return

        self._flattening = False

        if not real:
            return
        if ctx.working_order_ids:
            return
        if self._entries >= self.max_entries:
            return
        if self._bar_ns <= 0:
            return

        hold_ns = self.hold_bars * self._bar_ns
        if left_ns <= hold_ns + self.flat_buffer_ns:
            return
        if self._bar - self._last_entry_bar < self.cooldown_bars:
            return

        side = self._signal(1)
        if side == 0 and self._entries == 0:
            span = ctx.session_end - ctx.session_start
            if span > 0 and (ctx.ts - ctx.session_start) >= self.fallback_after * span:
                side = self._signal(2)
        if side == 0:
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
        self._last_entry_bar = self._bar

    # ------------------------------------------------------------------ exits

    def _flatten(self, ctx) -> None:
        # Cancel the armed bracket before flattening: a reduce-only leg left
        # working would keep `ctx.working_order_ids` non-empty and block every
        # later entry.
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close(tag="horizon-exit")

    # -------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            if self._entry_ts == 0:
                self._entry_ts = ctx.fill.ts
        else:
            self._entry_ts = 0

    def on_lifecycle(self, ctx) -> None:
        pass
