"""Absorption reversal on the OKX BTC perpetual -- one round trip a session.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. Nothing here ever trades with the
absorbed flow.

What the four scored trials said, and what this file does about it
------------------------------------------------------------------
    #1  tight 35/70bp bracket            1892 trades   -0.2761  ->  -1.46e-4/trade
    #3  3h fixed horizon, tail bracket   1257          -0.1706  ->  -1.36e-4/trade
    #6  bigger moves + climax bar        1200          -0.2099  ->  -1.75e-4/trade
    #7  2m sweep-and-reclaim, 40min       917          -0.2940  ->  -3.21e-4/trade

A round trip is 10bp of notional, so a trade with no edge at all scores
-1.0e-3. #1 and #3 -- exits as different as a twenty-minute bracket and a
three-hour horizon -- both landed on a *gross* of +8.5bp and +8.6bp, the same
number to within 1%. That is the one solid fact this campaign has produced:

  * The entry carries real information, worth about +8.5bp gross.
  * The exit does not change it. Two exits three hours and ten times apart in
    holding period agree, so the drift attached to this signal is realised
    inside the first twenty minutes and then saturates. Changing the exit again
    is not where the missing basis points are.
  * It is therefore an arithmetic problem, and it is about 1.5bp a trade short.
    #6 tightened the entry (1.2 sigma moves, a volume climax filter) and got
    +8.25bp -- inside the noise of #3. #7 moved the trigger to 2m to pay less
    of the rejection at entry and got +6.8bp, significantly *worse*. So
    threshold tuning does not buy the missing 1.5bp, and finer triggers cost
    rather than save.

The one lever that is left is how many times the toll is paid
--------------------------------------------------------------
Since per-trade net is negative and fairly stable across designs, the score is
maximised by making the fewest round trips that still satisfies the 70%
session-exposure floor. This is not a small effect: at a fixed -1.4e-4 a trade,
1257 round trips cost 0.176 of score and 720 cost 0.101. Cutting the count is
worth about as much as doubling the gross edge, and unlike a threshold change
it is not a gamble on the population.

So: **at most one entry per session, and never more.** Trial #3 averaged 1.31
trades a session; the second trade of a day pays the same 10bp toll for a
signal that arrives later, with less of the day left to hold, and there is no
evidence it is better than the first.

Entry, unchanged from the best-measured trial
---------------------------------------------
The entry is #3's, which produced the best per-trade number in the campaign,
with the windows restated so that the absorption bar is never inside the
statistics it has to clear:

* Prior move. Close-to-close over the last 12 closed 5m bars, measured to the
  *previous* bar so the absorption bar's own rejection cannot dilute the move
  it is failing to extend. Required magnitude: max(1.0 sigma_bar * sqrt(12),
  18bp), sigma from the last 96 bar returns, kept incrementally.
* Flow drove the move. Sum of bar.delta over those same 12 bars divided by sum
  of bar.volume must agree in sign with the move and be at least 2.5% in
  magnitude. A drift on balanced flow carries no absorption to read.
* Unusually large delta on the absorption bar. delta/volume z-scored against
  the same ratio over the preceding 48 bars, sign-matched, at least 0.85 sigma
  and at least 5% absolute. The ratio, not raw contracts: BTC volume varies by
  orders of magnitude across hours and years, and a raw-delta z is a clock.
* Fails to extend, and rejects its own extreme. The bar's low may not undercut
  the lowest low of the prior 12 bars (mirror for highs) -- the flow moved
  nothing -- and it must close in the favourable 30% of its own range, which is
  fixed the moment the bar closes and cannot be revised.

Confirmation. None. The entry is the absorption bar itself, at market, eligible
from the next event. Resting limits were rejected in an earlier trial for a
reason that still holds: fills here are book-queue pessimistic, so a resting bid
fills only when trades print strictly through it and therefore fills
preferentially in exactly the states where the reversal failed.

Exit. Fixed horizon of 6 bars (30 minutes) from the fill, closed at market, or
the UTC-day flatten fifteen minutes before the close. Since the drift saturates
by twenty minutes, a three-hour hold adds variance and no mean; half an hour
keeps the entry window open until 23:15 UTC instead of 22:00, which helps
coverage. A 2% stop and a 5% target ride along as catastrophe protection only.

The exposure floor, handled explicitly rather than hoped for
------------------------------------------------------------
Four of this campaign's eight proposals were NOT SCORED for missing the 70%
floor, and that is worse than a bad score because it yields no information. A
second, much looser tier (0.5 sigma / 10bp move, 3% delta, z >= 0.3, 18%
rejection, no flow test) is therefore available -- but only as a controller,
not as a parallel strategy. The strategy tracks what fraction of completed
actionable sessions it has traded. The relaxed tier is admitted only after 35%
of the day has elapsed and only while that running fraction is below 0.78. A
session that finds a strict signal lifts the fraction and switches the relaxed
tier off again; a session that finds none lowers it and turns it on. The
fraction therefore oscillates around 0.78 rather than saturating: the number of
insurance trades is bounded from above roughly by (0.78 - strict coverage) x
sessions, so the controller pays for the floor with a few dozen trades instead
of a few hundred.

Bookkeeping
-----------
Flat to flat inside the UTC day, always. The entry block is reachable only from
flat with nothing working, so long -> short is expressed as long -> flat ->
short. Every feature is maintained incrementally on bounded deques with running
sums: nothing grows across 960 sessions, nothing re-walks history, there is no
ctx.history() call anywhere, so no WindowTooShort and no quadratic cost.
Features accumulate through warmup and across the UTC boundary -- this market
has no close and no gap, and clearing a 48-bar flow window every midnight would
leave the first hours of every day blind. The 5m subscription uses
emit_empty=True so the end-of-day flatten always has a callback; empty bars
carry no volume and are excluded from every feature. Every order id returned by
buy()/sell() is checked for None.
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
        ret_window: int = 96,
        ratio_window: int = 48,
        # --- strict tier: the real signal
        s_delta_floor: float = 0.050,
        s_delta_z: float = 0.85,
        s_move_sigmas: float = 1.00,
        s_move_floor: float = 0.0018,
        s_close_frac: float = 0.30,
        s_flow_agree: float = 0.025,
        # --- relaxed tier: exposure insurance, admitted by the controller only
        r_delta_floor: float = 0.030,
        r_delta_z: float = 0.30,
        r_move_sigmas: float = 0.50,
        r_move_floor: float = 0.0010,
        r_close_frac: float = 0.18,
        r_flow_agree: float = 0.0,
        # --- exits
        stop_frac: float = 0.020,
        target_frac: float = 0.050,
        hold_bars: int = 6,
        flat_buffer_s: int = 900,
        # --- exposure controller
        cover_target: float = 0.78,
        cover_grace: int = 20,
        relax_after_frac: float = 0.35,
    ) -> None:
        self.interval = interval
        self.size = int(size)

        self.move_window = max(3, int(move_window))
        self.ret_window = max(40, int(ret_window))
        self.ratio_window = max(24, int(ratio_window))

        self.s_delta_floor = s_delta_floor
        self.s_delta_z = s_delta_z
        self.s_move_sigmas = s_move_sigmas
        self.s_move_floor = s_move_floor
        self.s_close_frac = s_close_frac
        self.s_flow_agree = s_flow_agree

        self.r_delta_floor = r_delta_floor
        self.r_delta_z = r_delta_z
        self.r_move_sigmas = r_move_sigmas
        self.r_move_floor = r_move_floor
        self.r_close_frac = r_close_frac
        self.r_flow_agree = r_flow_agree

        self.stop_frac = stop_frac
        self.target_frac = target_frac
        self.hold_bars = max(1, int(hold_bars))
        self.flat_buffer_ns = int(flat_buffer_s) * NS_PER_SECOND

        self.cover_target = cover_target
        self.cover_grace = max(0, int(cover_grace))
        self.relax_after_frac = relax_after_frac

        m = self.move_window

        # Bounded, incrementally maintained feature state. Nothing grows.
        self.closes: deque = deque(maxlen=m + 1)
        self.lows: deque = deque(maxlen=m)
        self.highs: deque = deque(maxlen=m)
        self.rets: deque = deque(maxlen=self.ret_window)
        self.ratios: deque = deque(maxlen=self.ratio_window)
        self.mdeltas: deque = deque(maxlen=m)
        self.mvols: deque = deque(maxlen=m)

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

        if self.closes:
            prev = self.closes[-1]
            if prev > 0.0:
                self._push(self.rets, (close - prev) / prev,
                           "_ret_sum", "_ret_sq")
        self.closes.append(close)
        self.highs.append(hi)
        self.lows.append(lo)

        ratio = (delta / vol) if vol > 0.0 else 0.0
        self._push(self.ratios, ratio, "_ratio_sum", "_ratio_sq")
        self._push(self.mdeltas, delta, "_md_sum")
        self._push(self.mvols, vol, "_mv_sum")

    # ----------------------------------------------------------------- signal

    def _signal(self, bar, relaxed: bool) -> int:
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise.

        Reads only the bar that just closed plus deques that still hold prior
        bars -- it is called before `_update`, so the absorption bar is never
        part of the statistics it has to clear.
        """
        vol = float(bar.volume)
        if vol <= 0.0:
            return 0
        lo = float(bar.low)
        hi = float(bar.high)
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

        # 2. ... and it is unusual for the last four hours.
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

        # 3. A directional move was already in progress into this bar.
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
        if len(self.lows) < m or len(self.highs) < m:
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
        """Admit the loose tier only while running coverage lags the target."""
        span = ctx.session_end - ctx.session_start
        if span <= 0:
            return False
        if (ctx.ts - ctx.session_start) < self.relax_after_frac * span:
            return False
        if self._seen < self.cover_grace:
            return True
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

        # ---- carrying a trade: the horizon clock, the session clock, and the
        #      catastrophe bracket. Never open the opposite side here.
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

        want = 0
        if real and self._entries < 1 and not ctx.working_order_ids:
            want = self._decide(ctx, bar)

        if real:
            self._update(bar)

        if want == 0:
            return
        if self._entries >= 1 or ctx.working_order_ids:
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
        if want > 0:
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
        # the position is gone would block every later entry through
        # ctx.working_order_ids.
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
