"""Cumulative volume delta continuation on the BTC perpetual.

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling keeps pushing price up for
a while, and aggressive selling that outweighs buying keeps pushing it down.
This file trades *with* the flow, never against it: a fade would be a different
hypothesis and is deliberately not implemented.

Measurement
-----------
Each bar carries its own signed order flow as `bar.delta` (buy_volume -
sell_volume), so no tick subscription is needed -- that matters, because a tick
subscription costs roughly 5x the wall clock and buys nothing here.

CVD is accumulated on `self` as a running sum of `bar.delta`. That is O(1) per
bar and never touches `ctx.history`, so there is no `WindowTooShort` risk and
nothing is recomputed over the whole history on every callback. A ring buffer
of the last `flow_window + 1` CVD values gives the flow over the last
`flow_window` bars as the difference between the newest and the oldest.

"Large" is the whole problem, because BTC volume differs across hours and
across years, so a fixed delta threshold means something different in 2023 and
in 2026. The normalisation here is a *z-score against the strategy's own
recent history*: the distribution of overlapping `flow_window`-bar CVD changes
over the previous `baseline` bars (20 days at 1h bars). That adapts to both the
volume regime and the price level, and it is computed from bars the engine has
already delivered, so it cannot reach forward.

Horizon
-------
The claim is about "the next comparable window", so the flow window and the
holding period are the same length: 24 one-hour bars. The position is opened at
the close of the bar whose flow crosses the threshold and flattened 24 bars
later with an explicit reduce-only market order. Entry is a market order
because the signal is about continuation -- waiting for a pullback with a
resting limit would fill only on the adverse move and would be a different,
slower signal.

Costs
-----
5 bp per side, 10 bp round trip, plus the spread paid through the touch. At a
day's horizon the typical move is a few hundred bp, so 10 bp is a tax rather
than a wall; at a five-minute horizon it is the whole game, which is why this
strategy does not trade intraday. Turnover is held down deliberately: one
position at a time, entries only on a *fresh* crossing of the threshold (the
previous bar's z must be inside it), and a cooldown after every completed
round trip. A signal that stays extreme for two days is one trade, not twelve.

Safety
------
A wide OCO bracket is attached to the entry as offsets, so the engine arms it
at the instant of the fill and it cannot invert against a slipped fill. The
stop is 1.5x the expected horizon move and the target 2.5x, so the horizon exit
does the work and the bracket is catastrophe insurance. The horizon exit
cancels every working order explicitly and then sends a `reduce_only` market
order rather than `close()`: if a bracket leg happens to fill at the same
instant the exit is merely rejected as a lifecycle event instead of opening a
new position on the other side. Order ids are never stored across callbacks --
everything is found by asking the engine what is working.

Sizing is a fixed whole number of lots, not a fixed notional. The run facts say
returns are scored relative to each session's opening price, which makes a
fixed lot size the consistent choice across a price that ran from $16k to
$100k; scaling lots by 1/price would quietly shrink the later years.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "1h",
        flow_window: int = 24,
        baseline: int = 480,
        min_baseline: int = 240,
        z_entry: float = 2.0,
        max_hold: int = 24,
        stop_mult: float = 1.5,
        target_mult: float = 2.5,
        vol_window: int = 48,
        size: int = 1000,
        cooldown: int = 12,
    ) -> None:
        self.interval = interval
        self.flow_window = int(flow_window)
        self.baseline = int(baseline)
        self.min_baseline = int(min_baseline)
        self.z_entry = float(z_entry)
        self.max_hold = int(max_hold)
        self.stop_mult = float(stop_mult)
        self.target_mult = float(target_mult)
        self.vol_window = int(vol_window)
        self.size = max(1, int(size))
        self.cooldown = int(cooldown)

        # Flow state. Everything here is derived from bars already delivered,
        # so it is a record of the past and nothing else.
        self.cvd = 0.0
        self.cvd_hist: deque[float] = deque(maxlen=self.flow_window + 1)
        self.flows: deque[float] = deque(maxlen=self.baseline)
        self.ranges: deque[float] = deque(maxlen=self.vol_window)
        self.prev_z = None

        # Position state, all reset on every flat fill.
        self.bars_held = 0
        self.exit_pending = False
        self.exit_wait = 0
        self.cool = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty keeps the hourly grid exact: a quiet hour contributes a
        # zero delta bar rather than shifting every subsequent window boundary.
        ctx.subscribe(self.interval, emit_empty=True)

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Accumulate unconditionally: warmup bars are real bars and the z-score
        # needs them just as much as the scored ones do.
        self.cvd += float(bar.delta)
        self.cvd_hist.append(self.cvd)
        self.ranges.append(max(0.0, float(bar.high) - float(bar.low)))

        z = self._flow_z()

        if not ctx.actionable:
            self.prev_z = z
            return

        if ctx.position != 0:
            self._manage(ctx)
            self.prev_z = z
            return

        # Flat: clear the trade state, then look for a fresh crossing.
        self.bars_held = 0
        self.exit_pending = False
        self.exit_wait = 0

        if self.cool > 0:
            self.cool -= 1
            self.prev_z = z
            return
        if ctx.working_order_ids:
            self.prev_z = z
            return
        if z is None:
            self.prev_z = z
            return

        prior = self.prev_z
        self.prev_z = z
        if prior is None:
            return

        # Only a *fresh* crossing counts. A flow that stays extreme for a day is
        # one trade, not one per bar -- turnover is where the 10 bp goes.
        if z >= self.z_entry and prior < self.z_entry:
            self._enter(ctx, 1)
        elif z <= -self.z_entry and prior > -self.z_entry:
            self._enter(ctx, -1)

    # ---------------------------------------------------------------- helpers

    def _flow_z(self):
        """Z-score of the last `flow_window`-bar CVD change against its own past."""
        if len(self.cvd_hist) < self.cvd_hist.maxlen:
            return None
        flow = self.cvd_hist[-1] - self.cvd_hist[0]

        z = None
        n = len(self.flows)
        if n >= self.min_baseline:
            s = 0.0
            ss = 0.0
            for f in self.flows:
                s += f
                ss += f * f
            mean = s / n
            var = ss / n - mean * mean
            if var > 0.0:
                sd = math.sqrt(var)
                if sd > 0.0:
                    z = (flow - mean) / sd

        self.flows.append(flow)
        return z

    def _sigma(self, px: float) -> float:
        """Rough scale of the move over the holding period: hourly range x sqrt(H)."""
        n = len(self.ranges)
        if n < 5:
            return px * 0.01
        mean_range = sum(self.ranges) / n
        if mean_range <= 0.0:
            return px * 0.01
        return mean_range * math.sqrt(self.max_hold)

    def _enter(self, ctx, direction: int) -> None:
        px = float(ctx.last_price)
        sigma = self._sigma(px)
        floor = px * 1e-4
        stop = max(sigma * self.stop_mult, floor)
        target = max(sigma * self.target_mult, floor)

        # Offsets, never absolute prices: the engine measures them from the
        # actual fill, so a slipped entry cannot put the stop on the wrong side.
        if direction > 0:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="cvd-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="cvd-short")

    def _manage(self, ctx) -> None:
        self.bars_held += 1
        if self.bars_held < self.max_hold:
            return
        if self.exit_pending:
            # An exit was already sent. Give it time to arrive (latency is
            # 100 ms) before sending another; reduce_only makes a duplicate
            # harmless, but a duplicate per bar is noise in the record.
            self.exit_wait += 1
            if self.exit_wait < 3:
                return
        self._flatten(ctx)
        self.exit_pending = True
        self.exit_wait = 0

    def _flatten(self, ctx) -> None:
        """Cancel what is resting, then reduce the position at market.

        Explicit cancels rather than cancel_all, and a reduce_only order rather
        than close(): if a bracket leg fills on the same tick the exit is
        rejected as a lifecycle event instead of flipping the book.
        """
        for order in ctx.find_orders():
            ctx.cancel(order.id)
        pos = int(ctx.position)
        if pos > 0:
            ctx.sell(size=abs(pos), reduce_only=True, tag="cvd-exit")
        elif pos < 0:
            ctx.buy(size=abs(pos), reduce_only=True, tag="cvd-exit")

    # -------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # ctx.position inside on_fill is the asset that just filled, which is
        # the perpetual. Flat means the round trip is over.
        if ctx.position == 0:
            self.bars_held = 0
            self.exit_pending = False
            self.exit_wait = 0
            self.cool = self.cooldown

    def on_lifecycle(self, ctx) -> None:
        # No order id is ever held across callbacks, so nothing here can go
        # stale. An order_expired or a rejected exit needs no cleanup.
        pass
