"""Absorption reversal on the OKX BTC perpetual -- one round trip per session,
and a coverage controller that may only ever close the gap to the registered
70% exposure floor, never overshoot it.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. Nothing in this file ever trades with
the absorbed flow.

What the campaign has actually established
------------------------------------------
    #1  tight 35/70bp bracket            1892 trades   -0.2761  ->  -1.46bp/trade
    #3  3h fixed horizon, tail bracket   1257          -0.1706  ->  -1.36bp/trade
    #6  larger moves + climax bar        1200          -0.2099  ->  -1.75bp/trade
    #7  2m sweep-and-reclaim, 40min       917          -0.2940  ->  -3.21bp/trade
    #8  2m, fallback tier                1604          -0.3082  ->  -1.92bp/trade
    #9  one entry/session + controller    748          -0.2377  ->  -3.18bp/trade

A round trip costs 10bp of commission on notional; a trade with no edge at all
scores -1.0e-3 (-10bp). Every scored trial has landed between -1.4bp and
-3.2bp a trade, which is to say: **the signal is real and worth about +8.5bp
gross, and the toll is 10bp.** #1 and #3 -- a twenty-minute tight bracket and a
three-hour horizon with an unreachable target -- agreed on that +8.5bp to
within one percent. Two exits that far apart do not agree unless the per-trade
result is set at the entry and the drift is realised early and then saturates.

So there are exactly two levers, and the campaign has already spent its trials
on the wrong one.

Wrong lever: change the exit, tighten the threshold, move to finer bars.
#6 tightened thresholds and got +8.25bp gross. #7 moved to 2m and got +6.8bp.
#8 went to 2m with a fallback and got +8.1bp. All inside the noise of #3 or
worse. Threshold tuning does not buy the missing 1.5bp.

Right lever: **how many times the toll is paid.** With a per-trade net that is
negative and stable, the score is a sum of negative numbers and is maximised by
the fewest round trips that still satisfies the 70% session-exposure floor.
That floor is 672 of 960 sessions. At -1.4bp a trade, 672 trades score -0.094;
1257 score -0.176. Cutting the count is worth roughly as much as doubling the
gross edge, and unlike a threshold change it is arithmetic, not a gamble on the
population.

Why #9, which tried the same thing, failed -- and what is different here
----------------------------------------------------------------------
#9 capped entries at one a session, and it is still the second-worst scored
run in the campaign. The cause is visible in its own numbers: 748 trades on 960
sessions means its *strict* tier covered well under 70% of days (its thresholds
were 1.0 sigma / 18bp move, 0.85 z, 2.5% flow -- roughly a third of #3's
population), so its controller had to fire constantly. Its controller aimed at
0.78 coverage with a 20-session grace period and no reference to the actual
floor, which means it was paying an insurance premium of about 0.28 x 960 = 270
relaxed round trips at roughly -6bp each. That is -0.16 of score spent buying a
floor it already had margin above.

This file does the opposite in every one of those places:

* The strict entry is **#3's**, verbatim in its thresholds -- 0.8 sigma /
  15bp move, 5% delta floor, 0.75 z, 2% flow agreement, prior 12-bar extreme,
  30% rejection close. That is the loosest strict tier in the campaign and the
  one measured at 1257 trades over 960 sessions, i.e. it fires on most days on
  its own. It is the only tier in this campaign with a per-trade number better
  than -1.5bp.
* The controller's target is **0.75**, five points above the floor and fifteen
  below #9's. Its cost is therefore bounded above by (0.75 - true strict
  coverage) x 960 trades: five sessions' worth if the strict tier covers 0.70,
  and **zero** if it covers 0.75 or more, which #3's population suggests it
  does.
* The insurance tier is only mildly looser than the strict one -- 0.6 sigma /
  10bp move, 4% delta floor, 0.5 z, 25% rejection close, and it still requires
  1% flow agreement, so it is still an absorption reversal and never a
  continuation. #9's fallback dropped the flow test altogether and went to 3%
  delta / 0.3 z: a different, much worse strategy wearing the same name.
* No grace-period trading. The first 20 actionable sessions are counted and
  nothing else; the insurance tier cannot fire before there is a measurement
  to justify it.

The entry, restated so that the absorption bar never enters its own statistics
---------------------------------------------------------------------------
Everything below is read from deques that hold only bars strictly *before* the
one being judged -- `_signal` runs before `_update` -- so no threshold can be
raised or lowered by the bar it has to clear.

Prior move. Close-to-close over the previous `move_window` = 12 closed 5m bars,
one hour, measured to the bar *before* the absorption bar. Required magnitude:
0.8 x sigma_bar x sqrt(12) from the last 96 bar returns, held incrementally,
with a 15bp floor so the threshold cannot collapse in a dead tape. Measured to
the prior bar deliberately: including the absorption bar's own close would mean
a bar that had already begun to reverse dilutes the very move it is supposed to
be absorbing, which biases the filter against the signal.

Flow drove the move. Sum of `bar.delta` over those same twelve bars divided by
the sum of `bar.volume` must agree in sign with the price move and be at least
2% in magnitude. The premise is about *aggressive* flow moving price; a drift
on balanced flow carries no absorption to read.

Unusually large delta on the absorption bar. `delta / volume` for the bar,
z-scored against the same ratio over the previous 48 bars, sign-matched, at
least 0.75 sigma and at least 5% absolute. The ratio rather than raw contracts,
because BTC volume differs by orders of magnitude across hours and across years
and a raw-delta z-score would mostly be a clock.

Fails to extend. The bar's low may not undercut the lowest low of the previous
twelve bars (mirror for highs) -- the direct reading of "the flow moved
nothing" -- and it must close in the favourable 30% of its own range, which is
fixed the moment the bar closes and cannot be revised.

Confirmation. None. The entry is the absorption bar itself, at market, eligible
from the next event. Resting limits were rejected earlier in the campaign for a
reason that still holds: fills here are book-queue pessimistic, so a resting bid
fills only when trades print strictly through it and therefore fills
preferentially in exactly the states where the reversal failed.

Exit. Fixed horizon of 12 bars (one hour) from the fill, closed at market, or
the UTC-day flatten with fifteen minutes to spare. A 2% stop and a 5% target
ride along as catastrophe protection only; both are unreachable inside an hour
of BTC and exist so no single bar can be a disaster. One hour rather than
#3's three is chosen for coverage, not for the drift: the drift saturates well
before it, so the mean is unaffected, while the entry window extends from
20:45 to 22:45 UTC and every signal in that extra window is a day that reaches
the exposure floor without an insurance trade.

Flat to flat inside the UTC day, always. The entry block is reachable only
from flat with nothing working, so long -> short is expressed as long -> flat
-> short and every trade opens and closes inside one UTC day. At most one entry
per session. The 5m subscription uses `emit_empty=True` so the end-of-day
flatten always has a callback to run in, even in a dead tape.

Robustness. Every feature is maintained incrementally on bounded deques with
running sums -- 13 closes, 12 extremes, 48 ratios, 12 deltas, 96 returns -- so
nothing grows across 960 sessions, nothing re-walks history, and there is no
`ctx.history()` call anywhere, so no `WindowTooShort` and no quadratic cost.
Features accumulate through warmup and deliberately persist across the UTC
boundary: this market has no close and no gap, and clearing a four-hour flow
window every midnight would leave the first hours of every day blind. Empty
`emit_empty` bars carry no volume, are excluded from every feature, and serve
only to drive the flatten. Every order id returned by `buy`/`sell` is checked
for `None`.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        size: int = 10,
        # feature windows, in bars of `interval`
        move_window: int = 12,
        ret_window: int = 96,
        ratio_window: int = 48,
        extreme_window: int = 12,
        # --- strict tier: #3's entry, the best-measured population in campaign
        s_move_sigmas: float = 0.80,
        s_move_floor: float = 0.0015,
        s_delta_floor: float = 0.050,
        s_delta_z: float = 0.75,
        s_close_frac: float = 0.30,
        s_flow_agree: float = 0.020,
        # --- insurance tier: mildly looser, still an absorption reversal
        r_move_sigmas: float = 0.60,
        r_move_floor: float = 0.0010,
        r_delta_floor: float = 0.040,
        r_delta_z: float = 0.50,
        r_close_frac: float = 0.25,
        r_flow_agree: float = 0.010,
        # --- exits
        stop_frac: float = 0.020,
        target_frac: float = 0.050,
        hold_bars: int = 12,
        flat_buffer_s: int = 900,
        # --- coverage controller
        cover_target: float = 0.75,
        cover_grace: int = 20,
        relax_after_frac: float = 0.55,
        max_entries: int = 1,
    ) -> None:
        self.interval = interval
        self.size = int(size)

        self.move_window = max(3, int(move_window))
        self.ret_window = max(40, int(ret_window))
        self.ratio_window = max(24, int(ratio_window))
        self.extreme_window = max(3, int(extreme_window))

        self.s_move_sigmas = float(s_move_sigmas)
        self.s_move_floor = float(s_move_floor)
        self.s_delta_floor = float(s_delta_floor)
        self.s_delta_z = float(s_delta_z)
        self.s_close_frac = float(s_close_frac)
        self.s_flow_agree = float(s_flow_agree)

        self.r_move_sigmas = float(r_move_sigmas)
        self.r_move_floor = float(r_move_floor)
        self.r_delta_floor = float(r_delta_floor)
        self.r_delta_z = float(r_delta_z)
        self.r_close_frac = float(r_close_frac)
        self.r_flow_agree = float(r_flow_agree)

        self.stop_frac = float(stop_frac)
        self.target_frac = float(target_frac)
        self.hold_bars = max(1, int(hold_bars))
        self.flat_buffer_ns = int(flat_buffer_s) * 1_000_000_000

        self.cover_target = float(cover_target)
        self.cover_grace = max(0, int(cover_grace))
        self.relax_after_frac = float(relax_after_frac)
        self.max_entries = max(1, int(max_entries))

        m = self.move_window

        # Bounded, incrementally maintained feature state. Nothing grows.
        self.closes: deque = deque(maxlen=m + 1)
        self.highs: deque = deque(maxlen=self.extreme_window)
        self.lows: deque = deque(maxlen=self.extreme_window)
        self.ratios: deque = deque(maxlen=self.ratio_window)
        self.mdeltas: deque = deque(maxlen=m)
        self.mvols: deque = deque(maxlen=m)
        self.rets: deque = deque(maxlen=self.ret_window)

        self._ret_sum = 0.0
        self._ret_sq = 0.0
        self._ratio_sum = 0.0
        self._ratio_sq = 0.0
        self._md_sum = 0.0
        self._mv_sum = 0.0

        # Session and trade bookkeeping.
        self._session = None
        self._counted = False
        self._traded = False
        self._entries = 0
        self._seen = 0
        self._traded_n = 0
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
        """Fold a *real* closed bar into the statistics. Called after _signal."""
        close = float(bar.close)
        vol = float(bar.volume)
        delta = float(bar.delta)

        if self.closes:
            prev = self.closes[-1]
            if prev > 0.0:
                self._push(self.rets, (close - prev) / prev,
                           "_ret_sum", "_ret_sq")

        self.closes.append(close)
        self.highs.append(float(bar.high))
        self.lows.append(float(bar.low))

        ratio = (delta / vol) if vol > 0.0 else 0.0
        self._push(self.ratios, ratio, "_ratio_sum", "_ratio_sq")
        self._push(self.mdeltas, delta, "_md_sum")
        self._push(self.mvols, vol, "_mv_sum")

    # ----------------------------------------------------------------- signal

    def _signal(self, bar, relaxed: bool) -> int:
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise.

        Reads only the bar that just closed plus deques that still hold bars
        strictly before it -- it is called before `_update`, so the absorption
        bar is never part of the statistics it has to clear.
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

        if relaxed:
            dfloor = self.r_delta_floor
            zthr = self.r_delta_z
            msig = self.r_move_sigmas
            mfloor = self.r_move_floor
            cfrac = self.r_close_frac
            fagree = self.r_flow_agree
        else:
            dfloor = self.s_delta_floor
            zthr = self.s_delta_z
            msig = self.s_move_sigmas
            mfloor = self.s_move_floor
            cfrac = self.s_close_frac
            fagree = self.s_flow_agree

        # 1. This bar's aggressive flow leans hard in one direction.
        dr = float(bar.delta) / vol
        if -dfloor < dr < dfloor:
            return 0
        long_side = dr < 0.0

        # 2. ... and that lean is unusual for the last four hours.
        n = len(self.ratios)
        if n < 30:
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
        start = self.closes[-1 - m]
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

    # ------------------------------------------------------- exposure control

    def _relaxed_allowed(self, ctx) -> bool:
        """Admit the insurance tier only while running coverage lags the floor.

        `_traded_n / _seen` is the fraction of *completed actionable* sessions
        on which anything was held. It is only ever allowed to pull coverage up
        to `cover_target`, which sits five points above the registered 70%
        floor, so the insurance tier costs at most (target - strict coverage) x
        sessions trades -- and nothing at all when the strict tier already
        covers the floor on its own.
        """
        span = ctx.session_end - ctx.session_start
        if span <= 0:
            return False
        if (ctx.ts - ctx.session_start) < self.relax_after_frac * span:
            return False
        if self._seen < self.cover_grace:
            return False
        return (self._traded_n / float(self._seen)) < self.cover_target

    def _decide(self, ctx, bar) -> int:
        side = self._signal(bar, False)
        if side != 0:
            return side
        if self._relaxed_allowed(ctx):
            return self._signal(bar, True)
        return 0

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]
        real = bar.tick_count > 0

        # ---- session boundary and coverage bookkeeping. Only actionable
        #      sessions are counted: warmup delivers real bars but nothing can
        #      be traded in them, so they must not dilute the fraction.
        if ctx.session != self._session:
            if self._session is not None and self._counted:
                self._seen += 1
                if self._traded:
                    self._traded_n += 1
            self._session = ctx.session
            self._counted = bool(ctx.actionable)
            self._traded = False
            self._entries = 0

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

        # ---- decide on the bar *before* folding it into the statistics, so a
        #      bar can never move the threshold it has to clear.
        side = 0
        if (real and self._entries < self.max_entries
                and not ctx.working_order_ids):
            side = self._decide(ctx, bar)

        if real:
            self._update(bar)

        if side == 0 or not real:
            return
        if ctx.working_order_ids or self._entries >= self.max_entries:
            return
        if self._bar_ns <= 0:
            return
        hold_ns = self.hold_bars * self._bar_ns
        if left_ns <= hold_ns + self.flat_buffer_ns:
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
        self._traded = True

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
        else:
            self._entry_ts = 0
            self._flattening = False

    def on_lifecycle(self, ctx) -> None:
        pass
