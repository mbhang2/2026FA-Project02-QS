"""Investigate whether an outsized intra-session move continues or gives back.

This strategy tests the give-back hypothesis with a session-relative trigger,
but addresses the prior failures by:

1. Using a longer rolling window (12 bars) for the typical range, making the
   trigger more stable.
2. Requiring a very strong directional close (top/bottom 15% of the bar's range)
   to filter out weak signals.
3. Trading only during a narrow window (first 90 minutes of the session) where
   prior trials suggested the effect might be most consistent.
4. Using a wider stop (4.0) and target (5.0) to give the reversion room.
5. Adding a minimum typical range to avoid trading in very quiet sessions.

All state is kept on self. Only the single bar that just closed is read from
history, so no WindowTooShort crash can occur.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 12,
        range_mult: float = 2.5,
        min_typical_range: float = 0.75,
        size: int = 1,
        stop_offset: float = 4.0,
        target_offset: float = 5.0,
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

        # Trade only during the first 90 minutes of the session:
        # 17:00-18:30 CT = 22:00-23:30 UTC
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if not (22 * 60 <= minutes_since_midnight < 23 * 60 + 30):
            return

        if len(self._ranges) < self.lookback:
            return

        typical_range = sum(self._ranges) / len(self._ranges)
        if typical_range <= 0 or typical_range < self.min_typical_range:
            return

        if bar_range < self.range_mult * typical_range:
            return

        bar_span = last.high - last.low
        if bar_span <= 0:
            return
        close_position = (last.close - last.low) / bar_span

        # Require a very strong directional close: top or bottom 15%
        if not (close_position > 0.85 or close_position < 0.15):
            return

        if ctx.position or ctx.working_order_ids:
            return

        # Give-back: outsized up bar -> short; outsized down bar -> long
        if close_position > 0.85 and last.close > last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-short",
            )
        elif close_position < 0.15 and last.close < last.open:
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
