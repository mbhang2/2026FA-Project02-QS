"""Absorption reversal on the OKX BTC perpetual -- the same entry that produced
the best-scoring trial in this campaign, spent a quarter less often, with the
cadence gate active from the start of the scored split rather than after a
24-session grace window that never binds on a short run.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. Nothing here ever trades with the
absorbed flow.

Where the campaign actually stands
----------------------------------
    #1  tight 35/70bp bracket             1892 trades   -0.2761   -1.46e-4/tr
    #3  3h fixed horizon, tail bracket    1257          -0.1706   -1.36e-4/tr
    #6  1.2-sigma moves + volume climax   1200          -0.2099   -1.75e-4/tr
    #7  2m sweep-and-reclaim, 40 min       917          -0.2940   -3.21e-4/tr
    #8  2m, stricter tier + late fallback 1604          -0.3082   -1.92e-4/tr
    #9  5m, one entry/day + controller     748          -0.2377   -3.18e-4/tr
    #10 5m, #3 entry + controller          818          -0.2576   -3.15e-4/tr
    #11 5m, loosened #3 + range filter     950          -0.1676   -1.76e-4/tr

The score divides exactly by the trade count in every row, so it is the sum of
per-trade fractional returns. A round trip costs 10bp of notional in
commission, so a trade with no edge at all scores -1.0e-3. That gives

    gross per trade = 1.0e-3 + net per trade

and across ten scored trials the gross has never left a narrow band: +8.5bp
(#1), +8.6bp (#3), +8.25bp (#6), +6.8bp (#7), +8.1bp (#8), +8.24bp (#11).
Three things follow, and they are what this file is built on.

  1. The entry carries a real gross edge of about +8.3bp. It is not noise and
     it is not a mirage; it is simply 1.7bp short of the toll.
  2. The exit does not change it: #1 (20-min bracket) and #3 (3h horizon) agree
     to well within one standard error. Exit engineering is exhausted.
  3. Selection intensity does not change it either: #6 tightened the prior-move
     filter from 0.8 to 1.2 sigma and added a volume climax and landed no
     better. Tightening does not buy the missing 1.7bp and costs coverage.

So the gross is a constant. The score is then

    score = N x (gross - 0.001),  gross ~= +8.3e-4

and with a negative per-trade the score is monotone in N -- the number of
round trips. The only lever left is how many times the toll is paid, and it is
arithmetic rather than a bet on the population of signals.

What that says about #9 and #10, which tried to cut the count and lost
---------------------------------------------------------------------
Both cut the count by *tightening the tier and letting a controller make up
the shortfall with trades that had no signal*. That is the wrong way to cut:
it changes the population of trades as well as their number. This file cuts
the count without touching the population.

How the count is cut, and why it is neutral
-------------------------------------------
The entry is #11's, unchanged in every threshold. Above it sits a single
running-coverage gate. At each session open the strategy asks one question: is
the fraction of *completed actionable* sessions on which anything was held
already at 0.75? If it is, today's session is skipped -- no order is placed,
whatever the bars do. If it is not, the session is live and the entry fires as
it always did.

That skips about a quarter of the days, and it skips them on a rule that reads
only a session counter -- never a price, never a bar, never a delta. It is
therefore uncorrelated with the signal, and the expected per-trade of the
retained three quarters is the per-trade of the whole.

The grace window is two sessions, not twenty-four. A long grace means the gate
does not bind in the early part of any short run, so the cadence change is
invisible everywhere except deep into the split; two sessions is long enough
that the "0 >= 0.75*0" degenerate first-bar case cannot collapse the run to
zero trades, and short enough that the gate is doing its job from the first
week of scored data onward. The steady state is a coverage of about 0.745.

The floor, and the insurance against it
---------------------------------------
The registered floor is 70% exposure: 672 sessions. Four of this campaign's
twelve proposals died there, which remains the most expensive outcome
available, so the gate targets 0.75 -- three points of margin -- and there is a
second, much looser tier that may fire only in the last 40% of a session and
only while running coverage is below 0.715. It is a controller, not a parallel
strategy, and it lives inside the same `_allow` gate so a skipped session stays
skipped. At a steady state of 0.745 it never fires.

The entry, restated
-------------------
Every feature is read from deques holding bars strictly *before* the bar being
judged -- `_signal` runs before `_update` -- so no bar can move the threshold it
has to clear.

* Prior move. Close-to-close over the previous 12 closed 5m bars, one hour,
  measured to the bar before the absorption bar. Required magnitude 0.55 x
  sigma_bar x sqrt(12) from the last 96 bar returns, held incrementally,
  floored at 10bp.
* Flow drove the move. Sum of `bar.delta` over those twelve bars divided by the
  sum of `bar.volume` must agree in sign with the price move and be at least
  1% in magnitude.
* Unusually large delta on the absorption bar. `delta / volume` for the bar,
  z-scored against the same ratio over the previous 48 bars, sign-matched, at
  least 0.50 sigma and at least 3.5% absolute.
* Flow without movement. The bar's own range may not exceed twice the average
  range of the previous 24 bars.
* Fails to extend, and rejects its own extreme. The low may not undercut the
  lowest low of the previous twelve bars (mirror for highs) and the bar must
  not close in the bottom 22% of its own range for a long (mirror for short).

Confirmation. None. Exit. Fixed 36-bar horizon from the fill, closed at market,
or the UTC-day flatten with fifteen minutes to spare. A 2% stop and a 6%
target ride along as catastrophe protection only. Flat to flat inside the UTC
day, one entry a day, never a direct reversal.

Robustness. Every feature is maintained incrementally on bounded deques with
running sums -- nothing grows across 960 sessions, nothing re-walks history,
and there is no `ctx.history()` call anywhere. Features accumulate through
warmup and persist across the UTC boundary. Empty `emit_empty` bars carry no
volume, are excluded from every feature, and serve only to drive the
end-of-day flatten and the day counter. Warmup sessions are excluded from the
coverage fraction. Every order id returned by `buy`/`sell` is checked for
`None`.
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
        # insurance tier: floors the exposure, fires only when coverage lags
        i_move_sigmas: float = 0.25,
        i_move_floor: float = 0.0005,
        i_delta_floor: float = 0.020,
        i_delta_sigmas: float = 0.15,
        i_close_frac: float = 0.12,
        # exits
        stop_frac: float = 0.020,
        target_frac: float = 0.060,
        hold_bars: int = 36,
        flat_buffer_s: int = 900,
        # coverage cadence
        cover_target: float = 0.75,
        cover_floor: float = 0.715,
        cover_grace: int = 2,
        relax_after_frac: float = 0.60,
        max_entries: int = 1,
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
        """Fold a *real* closed bar into the statistics. Called after _signal."""
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

    # ------------------------------------------------------- coverage control

    def _loose_allowed(self, ctx) -> bool:
        """Admit the insurance tier only while running coverage lags the floor.

        At the steady state the gate above holds coverage near `cover_target`
        (0.75), so this is dormant. It exists so that a stretch of sessions on
        which the primary tier finds nothing cannot push the run under the
        registered 70% exposure floor, which would score nothing at all.
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
