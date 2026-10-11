"""Investigate whether an outsized intra-session move continues or gives back.

This strategy measures "outsized" against the session's own realised range so
far, using a rolling window of recent bar ranges. It enters in the direction of
the outsized bar's close relative to its open, and lets a bracket manage the
exit.

The prior trial #3 traded 22,830 times and scored -0.8746, which suggests the
strategy was overtrading and the continuation claim was not supported. This
version addresses that by:

1. Requiring a larger outsized move (range_mult = 1.8 instead of 1.2) to reduce
   the number of trades.
2. Requiring confirmation: the outsized bar must close in the top or bottom
   quarter of its own range, not merely up or down. This filters out bars that
   had a wide range but closed near the middle, which are less directional.
3. Adding a minimum typical range to avoid trading in very quiet sessions.
4. Widening the stop and target slightly to give the continuation more room.

The effect is still claimed to be continuation, so the entry is in the
direction of the outsized bar.

All state is kept on self and only the single bar that just closed is read
from history, so no WindowTooShort crash can occur.
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
        stop_offset: float = 2.0,
        target_offset: float = 3.0,
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

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()

        history = ctx.history(self.interval)
        if not history:
            return
        last = history[-1]

        bar_range = last.high - last.low
        if bar_range > 0:
            self._ranges.append(bar_range)
            if len(self._ranges) > self.lookback:
                self._ranges.pop(0)

        # Trade for most of the session, but skip the first and last 30 minutes
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if 22 * 60 <= minutes_since_midnight < 22 * 60 + 30:
            return
        if 20 * 60 + 30 <= minutes_since_midnight < 21 * 60:
            return

        if len(self._ranges) < self.lookback:
            return

        typical_range = sum(self._ranges) / len(self._ranges)
        if typical_range <= 0 or typical_range < self.min_typical_range:
            return

        if bar_range < self.range_mult * typical_range:
            return

        # Confirmation: close must be in the top or bottom quarter of the bar
        bar_span = last.high - last.low
        if bar_span <= 0:
            return
        close_position = (last.close - last.low) / bar_span
        if close_position < 0.25 or close_position > 0.75:
            if ctx.position or ctx.working_order_ids:
                return

            if last.close > last.open:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="outsized-continuation-long",
                )
            elif last.close < last.open:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="outsized-continuation-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
