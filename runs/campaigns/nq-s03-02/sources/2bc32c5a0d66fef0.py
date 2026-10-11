"""Outsized-move continuation with strict multi-bar confirmation and adaptive sizing.

The prior continuation trials lost money, but the best result (#4) used a
confirmation filter and a moderate range multiplier. This strategy extends that
idea by requiring the outsized move to be confirmed by the next bar's close
continuing in the same direction, which should reduce false signals from
one-bar spikes. It also uses a smaller size (1 contract) but a wider stop and
target to let the continuation run, and it trades only during the core hours
of the session (23:00-19:00 UTC) to avoid the noisy open and close.

The key change from #4 is the confirmation bar: after an outsized bar with a
directional close, we wait one bar and check that the next bar's close is also
in the same direction relative to its own range. This filters out many
one-bar wonders that immediately reverse.

All state is kept on self. Only the single bar that just closed is read from
history, so no WindowTooShort crash can occur.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 6,
        range_mult: float = 1.8,
        min_typical_range: float = 0.5,
        size: int = 1,
        stop_offset: float = 2.5,
        target_offset: float = 4.0,
    ):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.min_typical_range = min_typical_range
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        self._session = None
        self._ranges: list[float] = []
        self._pending_signal: str | None = None  # "long" or "short" after outsized bar

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()
            self._pending_signal = None

        history = ctx.history(self.interval)
        if not history:
            return
        last = history[-1]

        bar_range = last.high - last.low
        if bar_range > 0:
            self._ranges.append(bar_range)
            if len(self._ranges) > self.lookback:
                self._ranges.pop(0)

        # Trade only during core hours: 23:00-19:00 UTC
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if not (23 * 60 <= minutes_since_midnight < 19 * 60):
            self._pending_signal = None
            return

        if len(self._ranges) < self.lookback:
            return

        typical_range = sum(self._ranges) / len(self._ranges)
        if typical_range <= 0 or typical_range < self.min_typical_range:
            self._pending_signal = None
            return

        # If we have a pending signal from the previous bar, check confirmation
        if self._pending_signal is not None:
            bar_span = last.high - last.low
            if bar_span <= 0:
                self._pending_signal = None
                return
            close_position = (last.close - last.low) / bar_span

            if ctx.position or ctx.working_order_ids:
                self._pending_signal = None
                return

            if self._pending_signal == "long" and close_position > 0.5 and last.close > last.open:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="outsized-confirmed-long",
                )
            elif self._pending_signal == "short" and close_position < 0.5 and last.close < last.open:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="outsized-confirmed-short",
                )
            self._pending_signal = None
            return

        # No pending signal: check for an outsized bar that sets one up
        if bar_range < self.range_mult * typical_range:
            return

        bar_span = last.high - last.low
        if bar_span <= 0:
            return
        close_position = (last.close - last.low) / bar_span

        # Require close in the top or bottom quarter to set up a signal
        if close_position > 0.75 and last.close > last.open:
            self._pending_signal = "long"
        elif close_position < 0.25 and last.close < last.open:
            self._pending_signal = "short"

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
