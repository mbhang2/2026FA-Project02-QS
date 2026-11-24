"""Absorption reversal on the OKX BTC perpetual -- pullback-in-trend filter.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. Long follows absorbed selling after a fall, short
follows absorbed buying after a rise. This file never trades with the absorbed
flow.

What the two scored trials actually said
----------------------------------------
This is the reading that drives the change, so it is worth stating plainly.

  #1  1892 trades, score -0.2761   ->  -0.2761 / 1892 = -1.46e-4 per trade
  #3  1257 trades, score -0.1706   ->  -0.1706 / 1257 = -1.36e-4 per trade

Two quite different exit rules -- a 35/70bp bracket held for under an hour, and
a three-hour fixed horizon with an effectively unreachable bracket -- produced
almost the same *per trade* result. That is not what a broken entry looks like;
a broken entry scatters. It is what a fixed per-trade cost looks like, with a
real but insufficient gross edge underneath it.

The cost is known exactly: 5bp a side, so a round trip is 1.0e-3 of notional,
and the score is a return relative to price, so 1892 round trips would have cost
about 1.89 and 1257 would have cost about 1.26 if the signal were worth nothing
at all. Observed was -0.28 and -0.17. The residual is positive in both cases and
similar in both cases: roughly +8.5bp of gross per trade. The signal is real and
it is about 1.5bp a trade short of paying for itself.

So this trial does not change the shape of the trade -- fixed two-hour horizon,
catastrophe-only bracket, at most two entries a session -- because both shapes
landed in the same place and neither is the problem. It changes *which* signals
are taken, looking for the 1.5bp.

What changed: the trend filter
------------------------------
Both prior trials took every absorption in either direction. The new condition
is that the reversal must be with the larger move, not against it:

  long  (absorbed selling after a fall)  only when close > SMA(96 closes)  -- 8h
  short (absorbed buying  after a rise)  only when close < SMA(96 closes)

A one-hour fall that has not broken the eight-hour mean is a pullback; the
absorption in it is a large passive buyer defending a level inside an uptrend,
and the reversal is a resumption. The same bar printed below the eight-hour mean
is a breakdown with a pause in it, and the reversal it claims is a knife catch.
That distinction is the one thing in the data that separates a dip from a
decline, and it is measured from closes already seen.

The 96-bar window is deliberately an 8-hour regime marker, three funding stamps,
not a microstructure number. Nothing in the filter looks at an extreme, a level
or a prior swing: it is the mean of the last 96 closes and the current close.

Two honest caveats. In a market that trended up over the sample this filter also
tilts the book long, so part of whatever it adds is drift rather than edge; if
the score moves in the right direction I will want to know which. And it removes
roughly half the raw signals, which is why the other thresholds are loosened
below -- see the exposure note.

Everything else the brief leaves open, held as before
-----------------------------------------------------
Prior move. Close-to-close over the last `move_window` = 12 closed 5m bars (one
hour), required to be at least max(0.7 * sigma_bar * sqrt(12), 0.15%), with
`sigma_bar` the standard deviation of the last 96 bar returns kept incrementally.
Loosened from 0.8 to 0.7 sigma to offset the new filter's selectivity.

Flow drove the move. Sum of `bar.delta` over the move window divided by the sum
of `bar.volume` over it must agree in sign with the price move and be at least
2%. The premise is about aggressive flow moving price; a drift on balanced flow
carries no absorption reading.

Unusually large delta on the absorption bar. `delta / volume` for the bar,
z-scored against the mean and standard deviation of the same ratio over the
preceding 48 bars -- a ratio, not raw contracts, because BTC volume swings by
orders of magnitude across hours and years and a raw-delta z-score is mostly a
clock. Absolute imbalance floor 4%, z threshold 0.6 (both loosened from 5% and
0.75 for the same exposure reason), and the z must lean the same way as the bar.

Fails to extend. The absorption bar must not undercut the lowest low of the
prior 12 bars (mirror for highs) -- the direct reading of "the flow moved
nothing" -- and must close in the favourable 25% of its own range, which is the
rejection of its own extreme and is fixed the moment the bar closes. Kept
because it is the claim's own test, and it is the rule that makes the short side
not simply a mirror of a rising market.

Confirmation. None. Entry at market on the close of the absorption bar, eligible
from the next event (T+1). Waiting a bar costs entry price and eats the horizon.

Exit. Fixed horizon of 24 bars (two hours) from the fill, or the UTC-day flatten
with 15 minutes to spare, whichever comes first. A 2% stop and a 6% target ride
along as catastrophe protection only: the round trip costs 10bp whatever the
holding period, so the drift has to be given room to accumulate, and a tight
stop on a mean-reversion entry is stopped out by exactly the flush the thesis
says is the trap being sprung (trial #1). `emit_empty=True` so the end-of-day
exit always has a callback to run in.

Flat to flat inside the UTC day. One position at a time; the entry block is only
reachable from flat with nothing working, so long -> short always passes through
flat. At most 2 entries a session with a 24-bar cooldown, so an ordinary session
gets one or two trades and the exposure floor stays satisfied -- the loosened
thresholds above exist precisely so that adding the trend filter does not drop
me under it, which is how trials #0 and #2 scored nothing at all.

Costs and funding. Orders are at market both ways. Resting limits were rejected
in an earlier trial for a good reason: fills here are book-queue pessimistic, a
resting bid fills only when trades print strictly through it, so the limit fills
preferentially in the states where the reversal failed. A 10bp round trip paid
at the touch is the cheaper of the two. Positions are flat by construction
before the UTC close, so no trade spans a funding stamp outside its own session
beyond the ones at 08:00 and 16:00 that a two-hour hold can cross; that is a
handful of basis points of expected carry against a 10bp toll and not the lever.

Robustness. Every feature is maintained incrementally on bounded deques with
running sums -- nothing grows across 960 sessions, nothing re-walks history, no
`ctx.history()` call at all, so no `WindowTooShort` and no quadratic cost.
Features accumulate during warmup as well as after it, so the first actionable
bar is not blind. The statistics persist across the UTC boundary on purpose:
this market has no close and no gap, and clearing a 24-hour flow window every
midnight would leave the first hours of every day untradable.
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
        flow_window: int = 48,
        move_window: int = 12,
        vol_window: int = 96,
        trend_window: int = 96,
        move_sigmas: float = 0.70,
        move_floor: float = 0.0015,
        delta_floor: float = 0.04,
        delta_sigmas: float = 0.60,
        close_frac: float = 0.25,
        flow_agree: float = 0.02,
        stop_frac: float = 0.020,
        target_frac: float = 0.060,
        hold_bars: int = 24,
        max_entries: int = 2,
        cooldown_bars: int = 24,
        flat_buffer_s: int = 900,
    ) -> None:
        self.interval = interval
        self.size = size
        self.flow_window = flow_window
        self.move_window = move_window
        self.vol_window = vol_window
        self.trend_window = trend_window
        self.move_sigmas = move_sigmas
        self.move_floor = move_floor
        self.delta_floor = delta_floor
        self.delta_sigmas = delta_sigmas
        self.close_frac = close_frac
        self.flow_agree = flow_agree
        self.stop_frac = stop_frac
        self.target_frac = target_frac
        self.hold_bars = hold_bars
        self.max_entries = max_entries
        self.cooldown_bars = cooldown_bars
        self.flat_buffer_ns = flat_buffer_s * NS_PER_SECOND

        # Bounded, incrementally maintained feature state. Nothing grows.
        self.closes: deque = deque(maxlen=max(vol_window + 1, move_window + 1))
        self.rets: deque = deque(maxlen=vol_window)
        self.highs: deque = deque(maxlen=move_window + 1)
        self.lows: deque = deque(maxlen=move_window + 1)
        self.ratios: deque = deque(maxlen=flow_window)
        self.mdeltas: deque = deque(maxlen=move_window)
        self.mvols: deque = deque(maxlen=move_window)
        self.trend_closes: deque = deque(maxlen=trend_window)

        self._ret_sum = 0.0
        self._ret_sq = 0.0
        self._ratio_sum = 0.0
        self._ratio_sq = 0.0
        self._md_sum = 0.0
        self._mv_sum = 0.0
        self._trend_sum = 0.0

        self.last_ratio = 0.0
        self.last_range = 0.0

        self._session = None
        self._entries = 0
        self._bar = 0
        self._last_entry_bar = -10 ** 9
        self._bar_ns = 0
        self._hold_ns = 0
        self._entry_ts = 0
        self._flattening = False

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty so the end-of-day flatten always has a callback to run in.
        ctx.subscribe(self.interval, emit_empty=True)

    # ---------------------------------------------------------------- helpers

    def _add(self, dq: deque, val: float, sname: str, sqname=None) -> None:
        """Append to a bounded deque, maintaining running sum (and squares)."""
        if len(dq) == dq.maxlen:
            old = dq[0]
            setattr(self, sname, getattr(self, sname) - old)
            if sqname is not None:
                setattr(self, sqname, getattr(self, sqname) - old * old)
        dq.append(val)
        setattr(self, sname, getattr(self, sname) + val)
        if sqname is not None:
            setattr(self, sqname, getattr(self, sqname) + val * val)

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
                self._add(self.rets, (close - prev) / prev,
                          "_ret_sum", "_ret_sq")
        self.closes.append(close)
        self.highs.append(hi)
        self.lows.append(lo)

        ratio = (delta / vol) if vol > 0.0 else 0.0
        self.last_ratio = ratio
        self._add(self.ratios, ratio, "_ratio_sum", "_ratio_sq")
        self._add(self.mdeltas, delta, "_md_sum")
        self._add(self.mvols, vol, "_mv_sum")
        self._add(self.trend_closes, close, "_trend_sum")

    # ----------------------------------------------------------------- signal

    def _signal(self) -> int:
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise."""
        if len(self.closes) < self.move_window + 1:
            return 0
        if len(self.highs) < self.move_window + 1:
            return 0
        if len(self.ratios) < self.flow_window:
            return 0
        if len(self.mdeltas) < self.move_window:
            return 0
        if len(self.rets) < 20:
            return 0
        if len(self.trend_closes) < self.trend_window:
            return 0

        rng = self.last_range
        if rng <= 0.0:
            return 0

        # The absorption bar's own flow, and which way it leans.
        dr = self.last_ratio
        if -self.delta_floor < dr < self.delta_floor:
            return 0
        long_side = dr < 0.0

        n = len(self.ratios)
        mean = self._ratio_sum / n
        var = self._ratio_sq / n - mean * mean
        if var <= 1e-12:
            return 0
        z = (dr - mean) / math.sqrt(var)
        if abs(z) < self.delta_sigmas:
            return 0
        if (z < 0.0) != long_side:
            return 0

        # The prior move, on closes.
        start = self.closes[-self.move_window - 1]
        if start <= 0.0:
            return 0
        move = (self.closes[-1] - start) / start
        if abs(move) < self.move_floor:
            return 0
        if (move < 0.0) != long_side:
            return 0

        # Volatility-scaled threshold on the same move.
        cn = len(self.rets)
        mr = self._ret_sum / cn
        vr = self._ret_sq / cn - mr * mr
        if vr <= 0.0:
            return 0
        thr = self.move_sigmas * math.sqrt(vr) * math.sqrt(self.move_window)
        if thr < self.move_floor:
            thr = self.move_floor
        if abs(move) < thr:
            return 0

        # The move must have been driven by aggressive flow in its direction.
        if self._mv_sum <= 0.0:
            return 0
        if abs(self._md_sum) / self._mv_sum < self.flow_agree:
            return 0
        if (self._md_sum < 0.0) != long_side:
            return 0

        # Trend alignment: a reversal is only taken with the eight-hour move.
        # A 1h fall above the 96-close mean is a pullback, not a breakdown.
        trend = self._trend_sum / self.trend_window
        close = self.closes[-1]
        if long_side:
            if close <= trend:
                return 0
        else:
            if close >= trend:
                return 0

        # Did the flow fail to extend price, and did the bar reject its own
        # extreme? Both are fixed at the close of the bar and cannot be revised.
        lows = tuple(self.lows)
        highs = tuple(self.highs)
        prior_lows = lows[:-1]
        prior_highs = highs[:-1]
        if not prior_lows or not prior_highs:
            return 0

        if long_side:
            if lows[-1] < min(prior_lows):
                return 0
            if (close - lows[-1]) / rng < self.close_frac:
                return 0
            return 1

        if highs[-1] > max(prior_highs):
            return 0
        if (highs[-1] - close) / rng < self.close_frac:
            return 0
        return -1

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            # A UTC boundary is a calendar convention on a market with no close
            # and no gap: the flow statistics carry over, only the per-session
            # entry budget resets.
            self._session = ctx.session
            self._entries = 0

        # Real bars only feed the features -- an emit_empty bar carries the
        # previous close forward at zero volume and would poison every ratio.
        if bar.tick_count > 0:
            self._bar += 1
            self._update(bar)

        if self._bar_ns == 0 and bar.close_ts > bar.open_ts:
            self._bar_ns = bar.close_ts - bar.open_ts
            self._hold_ns = self.hold_bars * self._bar_ns

        if not ctx.actionable:
            return

        remaining = ctx.session_end - ctx.ts
        pos = ctx.position
        if pos == 0:
            self._flattening = False

        # ---- carrying a trade: the hold clock and the session clock are the
        #      only exits, plus the catastrophe bracket. Never reverse here.
        if pos != 0:
            if self._flattening:
                return
            timed_out = (self._hold_ns > 0 and self._entry_ts > 0
                         and ctx.ts - self._entry_ts >= self._hold_ns)
            if timed_out or remaining <= self.flat_buffer_ns:
                self._flattening = True
                self._flatten(ctx)
            return

        if bar.tick_count <= 0:
            return
        if ctx.working_order_ids:
            return
        if self._entries >= self.max_entries:
            return
        if self._hold_ns <= 0:
            return
        if remaining <= self._hold_ns + self.flat_buffer_ns:
            return
        if self._bar - self._last_entry_bar < self.cooldown_bars:
            return

        side = self._signal()
        if side == 0:
            return

        price = ctx.last_price
        if price <= 0.0:
            return

        stop = price * self.stop_frac
        target = price * self.target_frac
        if side > 0:
            oid = ctx.buy(size=self.size, stop_offset=stop,
                          target_offset=target, tag="absorb-entry")
        else:
            oid = ctx.sell(size=self.size, stop_offset=stop,
                           target_offset=target, tag="absorb-entry")
        if oid is None:
            return
        self._entries += 1
        self._last_entry_bar = self._bar

    # ------------------------------------------------------------------ exits

    def _flatten(self, ctx) -> None:
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position:
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
