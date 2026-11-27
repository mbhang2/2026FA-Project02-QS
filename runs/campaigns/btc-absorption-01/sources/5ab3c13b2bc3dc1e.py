"""Absorption reversal on the OKX BTC perpetual -- fixed-horizon hold.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. Long follows absorbed selling after a fall, short
follows absorbed buying after a rise. Never trades with the absorbed flow.

What changed from the previous trial, and why
----------------------------------------------
The previous trial read the same signal but exited on a *tight* bracket --
35bp stop, 70bp target -- armed at the fill. It lost. The signal itself may of
course be worthless, but there is a specific and well-known way a tight stop
destroys a mean-reversion entry: the absorption print is frequently followed by
one last flush through the extreme before the turn, which is precisely the state
the tight stop converts into a realised loss. The trade is stopped at -45bp by
the very move the thesis says is the trap being sprung.

So this trial keeps the entry definition and changes only the exit. A trade is
now opened at market on the absorption bar and closed at market after a fixed
`hold_bars` (36 bars = 3 hours) or at the UTC-day flatten, with only a
catastrophic stop (2%) and an effectively unreachable target (6%) in between.

Why a fixed horizon is the right shape here. The round trip costs 10bp whatever
the holding period, so a trade has to capture more than 10bp of drift to be
worth taking. Under a decaying-drift model a longer hold weakly dominates: the
cumulative drift is non-decreasing in the horizon while the cost is fixed, so
waiting three hours to pay the same toll is never worse in expectation than
waiting one. It only adds variance, and 960 sessions is enough sample for a
per-trade Sharpe of 0.1 to show up. The alternative -- a tight target that
converts most of the drift into an early exit -- was already tried.

Signal, with every degree of freedom the brief leaves open decided and fixed
----------------------------------------------------------------------------
Prior move. Close-to-close over the last `move_window` (12) closed 5m bars --
one hour -- required to be at least `max(0.8 * sigma_bar * sqrt(12), 0.15%)`,
so the threshold scales with recent volatility but never degenerates in a dead
tape. `sigma_bar` is the standard deviation of the last `vol_window` (96) bar
returns, kept incrementally.

Flow drove the move. New in this trial, and the one entry-side addition: the
sum of `bar.delta` over the move window, divided by the sum of `bar.volume`
over the same window, must agree in sign with the price move and be at least
`flow_agree` (2%) in magnitude. The premise is about *aggressive* flow moving
price; a drift that happened on balanced flow carries no absorption reading to
test. The threshold is deliberately low so it removes the flat-flow cases
without cutting the number of trading days -- the brief requires trading on most
days and an exposure floor killed an earlier proposal.

Unusually large delta on the absorption bar. `delta / volume` for the bar,
z-scored against the mean and standard deviation of the same ratio over the
preceding `flow_window` (48) bars. The ratio rather than raw contracts, because
BTC volume swings by orders of magnitude across hours and across years and a
raw-delta z-score would mostly be a clock. A 5% absolute imbalance floor stops
a z-score in a quiet tape from firing on nothing.

Fails to extend, and closes back inside. Chosen rule: the absorption bar must
not undercut the lowest low of the prior 12 bars (mirror for highs), *and* it
must close in the upper (lower) 30% of its own range. The first is the direct
reading of "the flow moved nothing"; the second is the rejection of its own
extreme, which is measurable inside one bar and cannot be revised afterwards.
The alternative rule -- sweep the prior extreme and reclaim it -- was tried in
an earlier proposal and fired on 5 sessions out of 960, far under the exposure
floor, so it is not used.

Confirmation. None. The entry is the absorption bar itself, at market, eligible
from the next event (T+1). Waiting a bar costs entry price and shortens the
horizon available inside the UTC day.

Orders. Market in and market out. Resting limits were considered and rejected:
fills here are book-queue-pessimistic, a resting bid fills only when trades
print strictly through it, so a limit entry fills preferentially in exactly the
states where the reversal failed. Adverse selection on entry is a worse cost
than the touch, and the fixed horizon means we are paying the touch twice
regardless.

Flat to flat, within the UTC day. One position at a time; there is no path from
long to short that does not pass through flat, because the entry block is only
reachable when `ctx.position == 0` and no order is working. Entries are refused
with less than `hold_bars` plus a 15 minute buffer left in the session, and any
open position is cancelled-and-closed with 15 minutes left. The 5m subscription
uses `emit_empty=True` so that end-of-day exit always has a callback to run in,
even in a dead tape. At most `max_entries` (2) trades a session.

Robustness. All features are maintained incrementally on bounded deques with
running sums: nothing grows across 960 sessions and nothing re-walks history, so
no `WindowTooShort` and no quadratic cost. Warmup is skipped via
`ctx.actionable`. The statistics deliberately persist across the UTC boundary --
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
        move_sigmas: float = 0.8,
        move_floor: float = 0.0015,
        delta_floor: float = 0.05,
        delta_sigmas: float = 0.75,
        close_frac: float = 0.30,
        flow_agree: float = 0.02,
        stop_frac: float = 0.020,
        target_frac: float = 0.060,
        hold_bars: int = 36,
        max_entries: int = 2,
        flat_buffer_s: int = 900,
    ) -> None:
        self.interval = interval
        self.size = size
        self.flow_window = flow_window
        self.move_window = move_window
        self.vol_window = vol_window
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
        self.flat_buffer_ns = flat_buffer_s * NS_PER_SECOND

        # Bounded, incrementally maintained feature state.
        self.closes: deque = deque(maxlen=max(vol_window + 1, move_window + 1))
        self.rets: deque = deque(maxlen=vol_window)
        self.highs: deque = deque(maxlen=move_window + 1)
        self.lows: deque = deque(maxlen=move_window + 1)
        self.ratios: deque = deque(maxlen=flow_window)
        self.mdeltas: deque = deque(maxlen=move_window)
        self.mvols: deque = deque(maxlen=move_window)

        self._ret_sum = 0.0
        self._ret_sq = 0.0
        self._ratio_sum = 0.0
        self._ratio_sq = 0.0
        self._md_sum = 0.0
        self._mv_sum = 0.0

        self.last_ratio = 0.0
        self.last_range = 0.0

        self._session = None
        self._entries = 0
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

        # Did the flow fail to extend price, and did the bar reject its extreme?
        lows = tuple(self.lows)
        highs = tuple(self.highs)
        prior_lows = lows[:-1]
        prior_highs = highs[:-1]
        if not prior_lows or not prior_highs:
            return 0

        if long_side:
            if lows[-1] < min(prior_lows):
                return 0
            if (self.closes[-1] - lows[-1]) / rng < self.close_frac:
                return 0
            return 1

        if highs[-1] > max(prior_highs):
            return 0
        if (highs[-1] - self.closes[-1]) / rng < self.close_frac:
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

        if self._bar_ns == 0 and bar.close_ts > bar.open_ts:
            self._bar_ns = bar.close_ts - bar.open_ts
            self._hold_ns = self.hold_bars * self._bar_ns

        if not ctx.actionable:
            return

        remaining = ctx.session_end - ctx.ts
        pos = ctx.position
        if pos == 0:
            self._flattening = False

        # Real bars only feed the features; empty ones still drive the exit.
        if bar.tick_count > 0:
            self._update(bar)

        if pos != 0:
            if self._flattening:
                return
            timed_out = (self._hold_ns > 0 and self._entry_ts > 0
                         and ctx.ts - self._entry_ts >= self._hold_ns)
            if timed_out or remaining <= self.flat_buffer_ns:
                self._flattening = True
                self._flatten(ctx)
            return

        if ctx.working_order_ids:
            return
        if self._entries >= self.max_entries:
            return
        if self._hold_ns <= 0 or remaining <= self._hold_ns + self.flat_buffer_ns:
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
        if oid is not None:
            self._entries += 1

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
