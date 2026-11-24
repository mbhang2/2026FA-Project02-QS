"""Cumulative volume delta continuation on the OKX BTC perpetual.

The campaign's claim: unusually large aggressive net buying on BTC is followed
by higher prices over the next comparable window, and unusually large net
selling by lower prices. This strategy tests exactly that direction -- it never
fades a large flow.

Measurement
-----------
Each 5m bar already carries its order-flow parts (`bar.delta`, `bar.volume`),
so no tick subscription is needed and the walk stays cheap. Over a rolling
window of `cvd_bars` (24 bars = 2 hours) I accumulate

    cvd      = sum(delta)
    totvol   = sum(volume)
    ratio    = cvd / totvol          in [-1, +1], scale free

`ratio` is delta normalised by the volume that produced it, which is the
"how large is this flow" question the campaign asks to have answered against
something the data supplies rather than a fixed contract count.

"Unusually large" is then a z-score of `ratio` against the last `norm_bars`
values of itself (2016 bars = 7 days), maintained with running sums so the
cost per bar is O(1) rather than a reshape of history every callback. A
7-day window spans quiet weekends and busy weekdays, which is exactly the
non-stationarity a fixed threshold would trip over.

Entry: |z| >= 2.0 and the sign of `ratio` agreeing with z. Long on positive
flow, short on negative flow -- continuation, not reversal.

Exit: the bracket is armed by the engine at the instant the market entry
fills, so the position is never naked, and it is given as *offsets* rather
than absolute levels so a slip on a market order cannot invert a leg. The
target is 0.80% and the stop 0.50% of the fill price -- wide enough that a
10bp round-trip commission is a small fraction of the intended move rather
than the whole of it, which is the binding constraint on this dataset.

There is also a time stop: if neither leg has resolved after `max_hold`
bars (96 = 8 hours) the working bracket is cancelled and the position is
flattened at market. Without it a signal that goes nowhere blocks the
strategy for the rest of the run.

Notes on the mechanics
----------------------
- Stats are accumulated on every bar, warmup included, so the z-score is
  already calibrated when `ctx.actionable` first turns true.
- A guard cancels stragglers: if we are flat and something is still working
  for several bars, the entry never filled and the working set is cleared so
  the strategy cannot deadlock.
- `self.exiting` prevents queuing a second flatten while the first is in
  flight.
- Nothing reads more than the last 512 bars from the window, and nothing
  recomputes a feature over the whole history per callback.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        cvd_bars: int = 24,
        norm_bars: int = 2016,
        min_norm: int = 288,
        z_entry: float = 2.0,
        target_pct: float = 0.008,
        stop_pct: float = 0.005,
        max_hold: int = 96,
        size: int = 1,
        stale_bars: int = 4,
    ) -> None:
        self.interval = interval
        self.cvd_bars = cvd_bars
        self.norm_bars = norm_bars
        self.min_norm = min_norm
        self.z_entry = z_entry
        self.target_pct = target_pct
        self.stop_pct = stop_pct
        self.max_hold = max_hold
        self.size = size
        self.stale_bars = stale_bars

        # Rolling order-flow window.
        self.deltas: deque[float] = deque(maxlen=cvd_bars)
        self.volumes: deque[float] = deque(maxlen=cvd_bars)

        # Rolling distribution of the normalised flow ratio.
        self.ratios: deque[float] = deque(maxlen=norm_bars)
        self.rsum = 0.0
        self.rsq = 0.0

        # Position bookkeeping.
        self.bars_held = 0
        self.exiting = False
        self.flat_with_orders = 0

    # ------------------------------------------------------------------
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------
    def _push_ratio(self, value: float) -> None:
        if len(self.ratios) == self.ratios.maxlen:
            old = self.ratios[0]
            self.rsum -= old
            self.rsq -= old * old
        self.ratios.append(value)
        self.rsum += value
        self.rsq += value * value

    def _flow_stats(self):
        n = len(self.ratios)
        if n < 2:
            return None
        mean = self.rsum / n
        var = self.rsq / n - mean * mean
        if var <= 0.0:
            return None
        return mean, var ** 0.5

    # ------------------------------------------------------------------
    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # --- accumulate flow on every bar, actionable or not -----------
        self.deltas.append(float(bar.delta))
        self.volumes.append(float(bar.volume))
        if len(self.deltas) == self.cvd_bars:
            total = sum(self.volumes)
            if total > 0.0:
                self._push_ratio(sum(self.deltas) / total)

        # --- manage an open position -----------------------------------
        if ctx.position:
            self.flat_with_orders = 0
            self.bars_held += 1
            if self.bars_held >= self.max_hold and not self.exiting:
                self.exiting = True
                if ctx.working_order_ids:
                    ctx.cancel_all()
                ctx.close()
            return

        self.bars_held = 0
        self.exiting = False

        # A working entry that has not filled. Give it a few bars, then clear
        # it so a hung order cannot block the rest of the run.
        if ctx.working_order_ids:
            self.flat_with_orders += 1
            if self.flat_with_orders > self.stale_bars:
                ctx.cancel_all()
                self.flat_with_orders = 0
            return
        self.flat_with_orders = 0

        if not ctx.actionable:
            return
        if len(self.ratios) < self.min_norm:
            return

        stats = self._flow_stats()
        if stats is None:
            return
        mean, sd = stats
        ratio = self.ratios[-1]
        z = (ratio - mean) / sd

        price = ctx.last_price
        if price <= 0.0:
            return

        stop_off = price * self.stop_pct
        target_off = price * self.target_pct

        if z >= self.z_entry and ratio > 0.0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="cvd-long",
            )
            self.bars_held = 0
            self.flat_with_orders = 0
        elif z <= -self.z_entry and ratio < 0.0:
            ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="cvd-short",
            )
            self.bars_held = 0
            self.flat_with_orders = 0

    # ------------------------------------------------------------------
    def on_fill(self, ctx) -> None:
        # The bracket is armed by the engine at the instant of the entry fill,
        # so there is nothing to do here. Position state is read next callback.
        pass

    def on_lifecycle(self, ctx) -> None:
        # No order ids are kept across callbacks, so an expiry or an OCO
        # cancellation cannot leave a stale id behind.
        pass
