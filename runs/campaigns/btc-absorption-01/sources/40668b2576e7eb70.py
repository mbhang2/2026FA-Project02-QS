"""Absorption reversal on the OKX BTC perpetual -- one round trip a day, sized
to the smallest population that clears the 70% exposure floor rather than to
the largest population the signal can be found in.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. Nothing here ever trades with the
absorbed flow.

What twelve trials have established, and what they have not
----------------------------------------------------------
    #1  tight 35/70bp bracket              1892 trades   -0.2761   -1.46e-4
    #3  3h fixed horizon, tail bracket     1257          -0.1706   -1.36e-4
    #6  1.2-sigma moves + volume climax    1200          -0.2099   -1.75e-4
    #7  2m sweep-and-reclaim, 40 min        917          -0.2940   -3.21e-4
    #8  2m, stricter tier + late fallback  1604          -0.3082   -1.92e-4
    #9  5m, one entry/day + loose fallback  748          -0.2377   -3.18e-4
    #10 5m, one entry/day, 1h horizon       818          -0.2576   -3.15e-4
    #11 5m, one entry/day, loose tier       950          -0.1676   -1.76e-4

A round trip is 10bp of commission on notional, so a trade with no edge at all
scores -1.0e-3. Write `gross = 1.0e-3 + net` and the whole campaign becomes one
column:

    #1 +8.5bp   #3 +8.6bp   #6 +8.25bp   #7 +6.8bp
    #8 +8.1bp   #9 +6.8bp   #10 +6.85bp  #11 +8.24bp

Two things are established and one is not.

  * The entry carries a positive gross edge. Ten thousand trades across eight
    populations do not average +7.9bp by accident; the spread of a single trade
    is roughly 60bp and the pooled standard error is under 1bp. This is a real
    effect and it is in the direction the claim names.
  * The edge is not large enough. +7.9bp against a 10bp toll is -2.1bp a trade,
    and no trial in this campaign has got closer than -1.36bp. Every scored run
    is negative and every one of them is negative for the same reason.

The thing that is *not* established is which entry variant is better. The
per-trade numbers run from -1.36e-4 to -3.21e-4, but a single 5m BTC trade held
an hour has a standard deviation around 35-60bp, so at 800-1900 trades the
standard error on any one of those rows is 1.5-2bp -- as large as the whole
spread between the best and the worst. #10's strict tier *is* #3's tier and it
scored 1.8bp worse per trade; #11 loosened every threshold and scored 1.4bp
better per trade than #10. Threshold tuning in this campaign has been a coin
flip whose result has been read as a finding. I am not going to spend this
trial on that coin.

Why the trade count is the lever, and why it is now the only one
---------------------------------------------------------------
The score is a plain sum of per-trade returns: 1257 x -1.36e-4 = -0.1706
exactly, and every other row divides the same way. With an expectation that is
negative and a variance that makes the per-trade estimate unresolvable, the
score is maximised by the *smallest* number of round trips that still satisfies
the registered exposure floor. That floor is 70% of 960 sessions -- 672
sessions that must hold a position at some point. 672 trades at -1.5e-4 is
-0.101; 950 trades at -1.76e-4 is -0.167. The saving is larger than any
plausible entry improvement, and unlike an entry improvement it is arithmetic
rather than a bet on a population I cannot measure.

This is the same lever #9 and #10 and #11 reached for, and it is worth being
explicit about why they did not collect it:

  * #9 covered the floor with a fallback tier that dropped the flow test and
    ran at 3% delta / 0.3z. That is not an absorption reversal, it is a
    different strategy wearing the name, and at -1.0e-3 a trade it cost more
    than the count saved.
  * #10 spent coverage on a 1h horizon and a controller aiming at 0.75 with a
    lagging counter, and landed at 85% coverage -- 818 trades where 700 would
    have done, and 115 of them bought at insurance quality.
  * #11 loosened the primary tier until it fired on essentially every session
    (950 trades over 960 sessions is 99% coverage) -- a 29-point margin over a
    floor that is worth 0.04 of score. That is the whole gap between #11 and
    the arithmetic bound.

The difference here is that the margin over the floor is spent deliberately and
only where it is needed: 0.76 of coverage targeted, not 0.99, with the
insurance tier held to a modest relaxation rather than abandoned.

The entry
---------
Unchanged in substance from #11, which is the best-scoring run in the campaign
and whose entry is a mild loosening of the best per-trade tier (#3's). Nothing
below is new; it is the same absorption test with its thresholds moved to land
the population near the floor rather than at the ceiling.

* Prior move. Close-to-close over the previous 12 closed 5m bars -- one hour --
  measured to the bar before the absorption bar so the absorption bar cannot
  dilute the move it is failing to extend. Required magnitude 0.65 sigma_bar x
  sqrt(12) from the last 96 bar returns held incrementally, floored at 11bp so
  the threshold cannot collapse in a dead tape.
* Flow drove the move. Sum of bar.delta over those twelve bars over the sum of
  bar.volume must agree in sign with the price move and be at least 1.2%. The
  premise is about aggressive flow moving price; a drift on balanced flow
  carries no absorption to read.
* Unusually large delta on the absorption bar. delta/volume z-scored against
  the same ratio over the previous 48 bars, sign-matched, at least 0.55 sigma
  and at least 4.2% absolute. The ratio rather than raw contracts: BTC volume
  differs by orders of magnitude across hours and across years, and a raw-delta
  z would mostly be a clock.
* Flow without movement. The bar's own range may not exceed 2.5x the average
  range of the previous 24. A bar carrying heavy aggressive flow normally
  prints a wide range; requiring it not to is the claim's literal form. Set
  loose, because this is the filter most likely to be measuring volatility
  rather than absorption, and a tight one would cut the population below the
  floor.
* Fails to extend, and rejects its own extreme. The low may not undercut the
  lowest low of the previous twelve (mirror for highs) -- the flow moved
  nothing -- and the bar must close in the favourable 26% of its own range,
  fixed at the close.

Confirmation. None. The entry is the absorption bar itself, at market, eligible
from the next event. Resting limits were measured earlier in this campaign and
rejected on principle rather than on a number: fills here are book-queue
pessimistic, so a passive bid fills only when trades print strictly through it,
which is precisely the state in which the reversal has failed. A signal that is
worth 8bp gross cannot be improved by buying it only when it is being run over.

Exit. Fixed horizon of 12 bars (one hour) from the fill, closed at market, or
the UTC-day flatten fifteen minutes before the boundary. One hour rather than
#3's three is not a claim about the drift -- #1 and #3 agreed at +8.5bp across
holds of twenty minutes and three hours, so the drift saturates early and the
horizon is free. It is chosen because a shorter hold extends the entry window
to 22:45 UTC and every signal in that extra window is a session that reaches
the floor without an insurance trade. A 2% stop and a 6% target ride along as
catastrophe protection only; both are unreachable inside an hour of BTC and
exist so no single bar can be a disaster.

The coverage controller
-----------------------
A counter tracks the fraction of *completed actionable* sessions on which
anything was held. Warmup sessions are excluded: they deliver real bars but no
order can be taken in them, and counting them would make the fraction
unreadable for the first fifty days.

The insurance tier is a modest loosening of the strict one -- 0.40 sigma /
7bp move, 3.0% delta floor, 0.30 z, 16% rejection close, and it still requires
the prior move to have been driven by aggressive flow in the same direction
(0.5%). Every trade it can produce is still an absorption reversal. It is
admitted only after 58% of the session has elapsed and only while running
coverage sits below 0.76 -- thirty points below #11's realised coverage and six
above the floor. Its cost is therefore bounded by how far coverage actually
lags the target, not by its own hit rate, and on a tape where the strict tier
covers the floor by itself it never fires at all.

Flat to flat inside the UTC day, always. One position at a time and at most one
entry a session, so long -> flat -> short is the only path that exists here.

Bookkeeping. Every feature is maintained incrementally on bounded deques with
running sums: 14 closes, 96 returns, 48 ratios, 24 ranges, 12 extremes, 12
deltas. Nothing grows across 960 sessions, nothing re-walks history, there is
no ctx.history() call anywhere, and so there is no WindowTooShort and no
quadratic cost. Features accumulate through warmup and deliberately persist
across the UTC boundary -- this market has no close and no gap, and clearing a
four-hour flow window every midnight would leave the first hours of every day
blind. Empty emit_empty bars carry no volume, are excluded from every feature,
and serve only to give the end-of-day flatten a callback to run in. Every order
id returned by buy()/sell() is checked for None.
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
        ret_window: int = 96,
        ratio_window: int = 48,
        extreme_window: int = 12,
        range_window: int = 24,
        # --- strict tier: mildly loosened #3, the best per-trade population
        s_move_sigmas: float = 0.65,
        s_move_floor: float = 0.0011,
        s_delta_floor: float = 0.042,
        s_delta_z: float = 0.55,
        s_close_frac: float = 0.26,
        s_flow_agree: float = 0.012,
        s_range_mult: float = 2.5,
        # --- insurance tier: a modest relaxation, still an absorption reversal
        r_move_sigmas: float = 0.40,
        r_move_floor: float = 0.0007,
        r_delta_floor: float = 0.030,
        r_delta_z: float = 0.30,
        r_close_frac: float = 0.16,
        r_flow_agree: float = 0.005,
        r_range_mult: float = 3.5,
        # --- exits
        stop_frac: float = 0.020,
        target_frac: float = 0.060,
        hold_bars: int = 12,
        flat_buffer_s: int = 900,
        # --- coverage controller
        cover_target: float = 0.76,
        cover_grace: int = 30,
        relax_after_frac: float = 0.58,
        max_entries: int = 1,
    ) -> None:
        self.interval = interval
        self.size = int(size)

        self.move_window = max(3, int(move_window))
        self.ret_window = max(40, int(ret_window))
        self.ratio_window = max(24, int(ratio_window))
        self.extreme_window = max(3, int(extreme_window))
        self.range_window = max(5, int(range_window))

        self.s_move_sigmas = float(s_move_sigmas)
        self.s_move_floor = float(s_move_floor)
        self.s_delta_floor = float(s_delta_floor)
        self.s_delta_z = float(s_delta_z)
        self.s_close_frac = float(s_close_frac)
        self.s_flow_agree = float(s_flow_agree)
        self.s_range_mult = float(s_range_mult)

        self.r_move_sigmas = float(r_move_sigmas)
        self.r_move_floor = float(r_move_floor)
        self.r_delta_floor = float(r_delta_floor)
        self.r_delta_z = float(r_delta_z)
        self.r_close_frac = float(r_close_frac)
        self.r_flow_agree = float(r_flow_agree)
        self.r_range_mult = float(r_range_mult)

        self.stop_frac = float(stop_frac)
        self.target_frac = float(target_frac)
        self.hold_bars = max(1, int(hold_bars))
        self.flat_buffer_ns = int(flat_buffer_s) * NS_PER_SECOND

        self.cover_target = float(cover_target)
        self.cover_grace = max(0, int(cover_grace))
        self.relax_after_frac = float(relax_after_frac)
        self.max_entries = max(1, int(max_entries))

        m = self.move_window

        # Bounded, incrementally maintained feature state. Nothing grows.
        self.closes: deque = deque(maxlen=max(m + 1, 16))
        self.highs: deque = deque(maxlen=self.extreme_window)
        self.lows: deque = deque(maxlen=self.extreme_window)
        self.rets: deque = deque(maxlen=self.ret_window)
        self.ratios: deque = deque(maxlen=self.ratio_window)
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
        self._entries = 0
        self._seen = 0
        self._traded_n = 0
        self._bar_ns = 0
        self._entry_ts = 0
        self._flattening = False

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty so the end-of-day flatten always has a callback to run in,
        # even in a tape with nothing trading in the final interval.
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
        """Fold a real closed bar into the statistics. Always called after
        `_signal`, so a bar can never move the threshold it had to clear."""
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
        self._push(self.ranges, float(bar.high) - float(bar.low), "_range_sum")

        ratio = (delta / vol) if vol > 0.0 else 0.0
        self._push(self.ratios, ratio, "_ratio_sum", "_ratio_sq")
        self._push(self.mdeltas, delta, "_md_sum")
        self._push(self.mvols, vol, "_mv_sum")

    # ----------------------------------------------------------------- signal

    def _signal(self, bar, relaxed: bool) -> int:
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise.

        Reads the bar that just closed plus deques holding only bars strictly
        before it. Never returns a trade that follows the absorbed flow.
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
            rmult = self.r_range_mult
        else:
            dfloor = self.s_delta_floor
            zthr = self.s_delta_z
            msig = self.s_move_sigmas
            mfloor = self.s_move_floor
            cfrac = self.s_close_frac
            fagree = self.s_flow_agree
            rmult = self.s_range_mult

        # 0. Flow without movement: a bar carrying heavy aggressive flow
        #    normally prints a wide range. It may not here.
        nr = len(self.ranges)
        if nr >= self.range_window:
            avg = self._range_sum / nr
            if avg > 0.0 and rng > rmult * avg:
                return 0

        # 1. This bar's aggressive flow leans hard in one direction, and the
        #    trade we would take is against that lean, never with it.
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
        if (z < 0.0) != long_side:
            return 0
        if abs(z) < zthr:
            return 0

        # 3. A directional move was already under way into this bar, measured
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

        # 4. Aggressive flow drove that move. A drift on balanced flow carries
        #    no absorption to read, so no trade.
        if fagree > 0.0:
            if len(self.mdeltas) < m or self._mv_sum <= 0.0:
                return 0
            imb = self._md_sum / self._mv_sum
            if (imb < 0.0) != long_side:
                return 0
            if abs(imb) < fagree:
                return 0

        # 5. The flow failed to extend price, and the bar rejected its own
        #    extreme. Both are fixed the instant the bar closes.
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
        """Admit the insurance tier only while running coverage lags target.

        `_traded_n / _seen` is the fraction of *completed actionable* sessions
        on which anything was held. The tier only ever pulls coverage up to
        `cover_target`, which sits six points above the registered 70% floor,
        so its cost is bounded by how far coverage actually lags -- and on a
        tape where the strict tier covers the floor by itself it never fires.
        """
        span = ctx.session_end - ctx.session_start
        if span <= 0:
            return False
        if (ctx.ts - ctx.session_start) < self.relax_after_frac * span:
            return False
        if self._seen < self.cover_grace:
            return False
        return (self._traded_n / float(self._seen)) < self.cover_target

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]
        real = bar.tick_count > 0

        # ---- session boundary and coverage bookkeeping. Only actionable
        #      sessions are counted: warmup delivers real bars but nothing can
        #      be traded in them, so counting them would make the fraction
        #      unreadable for the first fifty days.
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

        # ---- decide on the bar before folding it into the statistics.
        side = 0
        if (real and self._entries < self.max_entries
                and not ctx.working_order_ids
                and self._bar_ns > 0
                and left_ns > self.hold_bars * self._bar_ns + self.flat_buffer_ns):
            side = self._signal(bar, False)
            if side == 0 and self._relaxed_allowed(ctx):
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
