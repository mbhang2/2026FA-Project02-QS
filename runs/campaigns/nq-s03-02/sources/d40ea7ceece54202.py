"""Mean-reversion after outsized move, with strict session-time filter.

The prior continuation trials (#3, #4, #8, #9) all lost money, and the
give-back trials (#5, #6, #7) also lost money. This suggests that neither
simple continuation nor simple give-back after any outsized bar is profitable
on this dataset.

This strategy takes a different approach: it trades the give-back hypothesis,
but only in the last two hours of the session, where the first attempt's
hourly pattern showed the strongest negative returns after outsized moves
(hours 20-21 UTC). The idea is that late in the session, outsized moves are
more likely to be temporary imbalances from position squaring or order flow
that will revert before the close.

Key design choices:
1. Give-back entry: after an outsized up bar, sell short; after an outsized
   down bar, buy.
2. Restrict trading to 20:00-22:00 UTC (the last two hours of the session).
3. Require the outsized bar to close in the top or bottom quarter of its
   range, confirming a directional push that may have exhausted itself.
4. Use a modest range multiplier (1.5) to capture more signals, but only in
   the restricted time window.
5. Tight bracket (stop 1.5, target 2.0) to capture quick reversion.

All state is kept on self. Only the single bar that just closed is read from
history, so no WindowTooShort crash can occur.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        range_mult: float = 1.5,
        min_typical_range: float = 0.4,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 2.0,
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

        # Restrict to the last two hours of the session: 20:00-22:00 UTC
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if not (20 * 60 <= minutes_since_midnight < 22 * 60):
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

        if ctx.position or ctx.working_order_ids:
            return

        # Give-back: fade the outsized move
        if close_position > 0.75 and last.close > last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-short",
            )
        elif close_position < 0.25 and last.close < last.open:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
