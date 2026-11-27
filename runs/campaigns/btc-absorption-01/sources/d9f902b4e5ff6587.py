"""Absorption reversal on the OKX BTC perpetual -- one round trip a session,
at the best entry quality this campaign has measured.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. Nothing here ever trades with the
absorbed flow.

What the six scored trials actually said
----------------------------------------
    #1  tight 35/70bp bracket             1892 trades   -0.2761   -1.46e-4/trade
    #3  3h fixed horizon, tail bracket    1257          -0.1706   -1.36e-4/trade
    #6  1.2-sigma moves + volume climax   1200          -0.2099   -1.75e-4/trade
    #7  2m sweep-and-reclaim, 40 min       917          -0.2940   -3.21e-4/trade
    #8  2m, stricter tier + late fallback 1604          -0.3082   -1.92e-4/trade
    #9  5m, one entry/day + controller     748          -0.2377   -3.18e-4/trade

Score is additive in per-trade P&L: 1257 trades at -1.36e-4 reproduces -0.1706
exactly, and every other row divides the same way. A round trip is 10bp of
notional, so a trade with no edge at all is -1.0e-3. That fixes the whole
picture in one line:

    gross per trade = 1.0e-3 + net per trade

#1 and #3 -- a twenty-minute bracket and a three-hour horizon with an
unreachable target -- sit at -1.46e-4 and -1.36e-4, i.e. a gross of +8.5bp and
+8.6bp, agreeing to within 1% across exits that could hardly be more different.
Three conclusions follow, and they are the ones this file is built on.

  1. The entry carries a real, positive gross edge of about +8.6bp. This is not
     a signal with no information.
  2. The exit does not change it. Two exits ten times apart in holding period
     produce the same number, so the drift is realised early and then
     saturates. Exit engineering is exhausted; #1, #3 and #9 between them have
     tried brackets, fixed horizons and horizons a sixth as long.
  3. It is short by about 1.4bp a trade, and threshold tuning does not buy it.
     #6 tightened the move filter and added a volume climax and landed inside
     the noise of #3; #7 moved to 2m and lost 1.8bp. Selection at 5m is
     saturated.

The one lever left is how many times the toll is paid
-----------------------------------------------------
At a fixed net per trade the score is proportional to the number of round
trips, and nothing else. 1257 tolls cost 0.171; 700 cost 0.095. Halving the
count is worth more than doubling the gross edge, and unlike a threshold change
it is arithmetic rather than a gamble on the population of signals.

Trial #9 tried exactly this and lost -- but it changed three things at once: a
tighter strict tier (1.0 sigma / 18bp moves, z 0.85), a 30-minute horizon
instead of three hours, and a fallback tier that bought coverage with trades
that had no signal at all. Its per-trade fell from -1.36e-4 to -3.18e-4, so the
saving was swamped. Decomposing it: if roughly 115 of its 748 trades were
zero-edge fallback trades at -1.0e-3, the remaining 633 cost -2.3e-4 each --
still worse than #3, because the strict tier had been tightened and the horizon
shortened at the same time.

So this trial changes one thing: it keeps the entry that measured best and caps
it at a single entry per session, and it adjusts thresholds only in the
direction needed to keep the exposure floor satisfied.

The entry, and the one deliberate relaxation
--------------------------------------------
#3's entry is used, with every threshold loosened by roughly a third. That is
not a claim that loose is better -- #6 says tightening does not help either --
it is what makes one-entry-per-session viable. #3 fired 1.31 times a session,
so its per-session hit rate was somewhere between 66% and 100%; capping it at
one would put the score near -0.09 in the good case and below the 70% floor in
the bad one, and four of this campaign's ten proposals already died on that
floor, which is the most expensive outcome available. Loosening the primary
tier lifts the per-session hit rate without introducing any trade that is not
an absorption reversal, and the cap then bounds the total at 960 regardless.

* Prior move. Close-to-close over the previous 12 closed 5m bars -- one hour --
  measured to the bar before the absorption bar so the absorption bar cannot
  dilute the move it is failing to extend. Required magnitude 0.55 sigma_bar *
  sqrt(12), floored at 10bp so the threshold cannot collapse in a dead tape.
* Flow drove the move. Sum of bar.delta over those 12 bars divided by the sum
  of bar.volume must agree in sign with the price move and be at least 1.0% in
  magnitude. A drift on balanced flow carries no absorption to read.
* Unusually large delta on the absorption bar. delta/volume z-scored against the
  same ratio over the preceding 48 bars, sign-matched, at least 0.50 sigma and
  at least 3.5% absolute. The ratio rather than raw contracts: BTC volume
  differs by orders of magnitude across hours and years, and a raw-delta z is a
  clock.
* Flow without movement. New here, and the sharpest reading of the word
  "absorption": the bar's own range may not exceed twice the average range of
  the previous 24 bars. A bar carrying heavy aggressive flow normally prints a
  wide range; requiring it not to is the claim's literal form, and it is the
  one part of the premise no earlier trial measured.
* Fails to extend, and rejects its own extreme. The low may not undercut the
  lowest low of the previous 12 bars (mirror for highs) -- the flow moved
  nothing -- and the bar must close in the favourable 22% of its own range,
  which is fixed the moment the bar closes and cannot be revised.

Confirmation. None. The entry is the absorption bar itself, at market, eligible
from the next event. Waiting a bar has not been measured in this campaign and
would cost entry price on a drift that saturates inside twenty minutes.

Exit. Unchanged from the best-measured trial: a fixed horizon of 36 bars (three
hours) from the fill, closed at market, or the UTC-day flatten with fifteen
minutes to spare. A 2% stop and a 6% target ride along as catastrophe
protection only -- both are far outside anything that happens in three hours of
BTC and exist so no single bar can be a disaster.

The exposure floor, insured but not paid for
--------------------------------------------
A coverage counter tracks what fraction of completed actionable sessions have
traded. A second, much looser tier is admitted only when the session is 60% gone
*and* that running fraction is below 72%. It is a controller, not a parallel
strategy: when coverage is healthy it never fires, so its cost is bounded by how
far coverage actually lags rather than by its own hit rate. Warmup sessions are
excluded from both counts -- they deliver real bars but nothing can be traded in
them, and counting them would make the fraction unreadable.

Flat to flat inside the UTC day, always. One position at a time and at most one
entry a day, so long -> flat -> short is the only path there is.

Bookkeeping. Every feature is maintained incrementally on bounded deques with
running sums: 97 closes, 96 returns, 48 ratios, 24 ranges, 12 extremes, 12
deltas. Nothing grows across 960 sessions, nothing re-walks history, and there
is no ctx.history() call anywhere. Features accumulate through warmup and across
the UTC boundary -- this market has no close and no gap, and clearing a
four-hour flow window every midnight would leave the first hours of every day
blind. Empty bars carry no volume and are excluded from every feature; they
still provide the callback the end-of-day flatten needs, which is why the 5m
subscription uses emit_empty=True. Every order id returned by buy()/sell() is
checked for None.
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
        # primary tier: #3's entry, loosened enough that one trade a day covers
        move_sigmas: float = 0.55,
        move_floor: float = 0.0010,
        delta_floor: float = 0.035,
        delta_sigmas: float = 0.50,
        close_frac: float = 0.22,
        flow_agree: float = 0.010,
        range_mult: float = 2.0,
        # late-session insurance tier: coverage controller only
        insure_after_frac: float = 0.60,
        cover_floor: float = 0.72,
        cover_grace: int = 20,
        i_move_sigmas: float = 0.25,
        i_move_floor: float = 0.0005,
        i_delta_floor: float = 0.020,
        i_delta_sigmas: float = 0.15,
        i_close_frac: float = 0.12,
        # exits
        stop_frac: float = 0.020,
        target_frac: float = 0.060,
        hold_bars: int = 36,
        max_entries: int = 1,
        flat_buffer_s: int = 900,
    ) -> None:
        self.interval = interval
        self.size = int(size)

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

        self.insure_after_frac = float(insure_after_frac)
        self.cover_floor = float(cover_floor)
        self.cover_grace = max(0, int(cover_grace))
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

        # 0. Flow without movement: the bar may not be a wide-range bar.
        nr = len(self.ranges)
        if nr >= self.range_window:
            avg = self._range_sum / nr
            if avg > 0.0 and rng > self.range_mult * avg:
                return 0

        # 1. This bar's aggressive flow leans hard in one direction.
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

    # ------------------------------------------------------- exposure control

    def _loose_allowed(self, ctx) -> bool:
        """Admit the insurance tier only while running coverage lags the floor."""
        span = ctx.session_end - ctx.session_start
        if span <= 0:
            return False
        if (ctx.ts - ctx.session_start) < self.insure_after_frac * span:
            return False
        if self._seen < self.cover_grace:
            return True
        return (self._traded_n / float(self._seen)) < self.cover_floor

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

        # ---- decide on the bar before folding it into the statistics, so a
        #      bar can never raise the threshold it has to clear.
        side = 0
        if (real and self._entries < self.max_entries
                and not ctx.working_order_ids
                and self._bar_ns > 0
                and left_ns > self.hold_bars * self._bar_ns + self.flat_buffer_ns):
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
            self._traded = True
        else:
            self._entry_ts = 0
            self._flattening = False

    def on_lifecycle(self, ctx) -> None:
        pass
