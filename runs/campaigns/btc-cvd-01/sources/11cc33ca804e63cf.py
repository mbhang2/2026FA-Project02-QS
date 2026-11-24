"""CVD continuation on the OKX BTC perpetual.

The claim under test: aggressive buying (positive cumulative volume delta)
predicts further increases over the next comparable window, and aggressive
selling predicts further decreases. This file takes the claim literally -- it
does not fade. If continuation is wrong here, the campaign should find that out
rather than quietly rotate to the other side.

Measurement.  ``bar.delta`` is ``buy_volume - sell_volume`` per bar, so a running
sum over the last ``flow_lookback`` bars is CVD in lots. Raw CVD is not
comparable across hours or years -- BTC volume changes by an order of magnitude
over the split -- so the signal is the *ratio* of CVD to total volume over the
same window: the fraction of traded volume that was net aggressive, roughly in
[-1, 1]. That ratio is then standardised against its own recent distribution
(``stat_window`` bars), so "large" means large relative to what this instrument
has recently been doing rather than a fixed number of lots. Both pieces of state
are maintained incrementally -- the flow window is a deque with running sums, the
standardiser is a deque with running sum and sum-of-squares -- so no callback
recomputes anything over history. This matters: the run is 960 sessions.

Horizon.  A 30-minute bar, a 16-bar (8 hour) flow window, and a maximum hold of 8
bars (4 hours). The claim names "the next comparable window", so the holding
period is of the same order as the measurement window. Nothing here is a one-bar
signal: at five basis points a side plus the spread, a round trip costs roughly
0.10-0.12%, and only a multi-hour move has a chance of clearing that.

Entries fire on a *crossing* of the threshold, not while it stays above it. One
flow impulse should be one trade, not one trade per bar for as long as the
imbalance persists; that also throttles turnover, which is the dominant way a
flow signal dies here.

Exits are a fixed maximum hold, a decay of the signal (the z-score back near
zero, or flipped against the position), and a wide disaster stop. There is no
bracket: a bracket's stop and target are fixed distances from the fill that would
fight a time-based exit, and both legs would be armed and cancelled around a
decision this strategy makes on bars anyway. Orders are market orders -- the
entry has to happen at the instant the imbalance is measured, and a resting limit
would only fill if price first came back through it, which for a continuation
signal is precisely the case where the signal has failed.

Sizing.  1000 lots of 0.0001 BTC = 0.1 BTC, small enough that the top five levels
absorb it without walking the book and small enough that P&L is a clean linear
function of the price move.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Cumulative volume delta continuation, normalised and standardised."""

    def __init__(
        self,
        interval: str = "30m",
        flow_lookback: int = 16,
        stat_window: int = 240,
        z_entry: float = 2.5,
        z_exit: float = 0.75,
        max_hold: int = 8,
        min_hold: int = 2,
        stop_frac: float = 0.02,
        size: int = 1000,
    ) -> None:
        self.interval = interval
        self.flow_lookback = flow_lookback
        self.stat_window = stat_window
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.max_hold = max_hold
        self.min_hold = min_hold
        self.stop_frac = stop_frac
        self.size = size

        # Flow window: (delta, volume) per bar, with running sums kept in step.
        self._flow: deque = deque()
        self._cvd = 0.0
        self._vol = 0.0

        # Standardiser over the flow ratio itself.
        self._ratios: deque = deque()
        self._rsum = 0.0
        self._rsq = 0.0

        self.z = None
        self.prev_z = None
        self.bars_held = 0

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        # Several subscriptions can close in one callback; act only on ours.
        if self.interval not in ctx.bars:
            return
        self._ingest(ctx.bars[self.interval])

        # Warmup bars are real but orders would not be. Do not count them as
        # ignored actions; just keep the feature spinning up.
        if not ctx.actionable:
            return

        position = ctx.position
        if position != 0:
            self.bars_held += 1
            self._manage(ctx, position)
            return

        self.bars_held = 0
        if ctx.working_order_ids:
            return

        z, prev = self.z, self.prev_z
        if z is None or prev is None:
            return

        # Crossing semantics: the impulse is one trade, not one per bar.
        if z >= self.z_entry and prev < self.z_entry:
            ctx.buy(size=self.size, tag="cvd-long")
        elif z <= -self.z_entry and prev > -self.z_entry:
            ctx.sell(size=self.size, tag="cvd-short")

    def _manage(self, ctx, position: int) -> None:
        # If a close is already queued, let it land rather than stacking orders.
        if ctx.working_order_ids:
            return

        entry = ctx.average_entry_price
        last = ctx.last_price
        if entry > 0.0 and last > 0.0:
            direction = 1.0 if position > 0 else -1.0
            move = (last / entry - 1.0) * direction
            if move <= -self.stop_frac:
                ctx.close()
                return

        if self.bars_held >= self.max_hold:
            ctx.close()
            return

        if self.bars_held >= self.min_hold and self.z is not None:
            # For a long, the held side of the signal decaying (or flipping) is
            # a directional z at or below the exit threshold; for a short it is
            # -z. Written this way so a strong flip also exits.
            directional = self.z if position > 0 else -self.z
            if directional <= self.z_exit:
                ctx.close()

    # ---------------------------------------------------------------- feature

    def _ingest(self, bar) -> None:
        """Roll the flow window and the standardiser forward by one bar."""
        delta = float(bar.delta)
        volume = float(bar.volume)

        self._flow.append((delta, volume))
        self._cvd += delta
        self._vol += volume
        if len(self._flow) > self.flow_lookback:
            old_delta, old_volume = self._flow.popleft()
            self._cvd -= old_delta
            self._vol -= old_volume

        self.prev_z = self.z

        if len(self._flow) < self.flow_lookback or self._vol <= 0.0:
            self.z = None
            return

        ratio = self._cvd / self._vol

        self._ratios.append(ratio)
        self._rsum += ratio
        self._rsq += ratio * ratio
        if len(self._ratios) > self.stat_window:
            old = self._ratios.popleft()
            self._rsum -= old
            self._rsq -= old * old

        n = len(self._ratios)
        if n < self.stat_window:
            self.z = None
            return

        mean = self._rsum / n
        var = self._rsq / n - mean * mean
        if var <= 0.0:
            self.z = None
            return

        self.z = (ratio - mean) / (var ** 0.5)

    # ------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
