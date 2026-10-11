"""Give-back after an outsized intra-session move, with session-trend filter.

The prior continuation trials (#3, #4, #11, #12) all lost money. This strategy
tests the opposite hypothesis: after an outsized move, price gives back toward
its origin. It enters against the outsized bar's direction, but only when the
session's longer trend is also against that move, making the give-back a
counter-trend trade within a larger reversal context.

Key differences from prior trials:
- Direction is *opposite* the outsized bar (give-back, not continuation).
- Requires the outsized bar to close in the top/bottom quarter of its range,
  indicating a strong directional thrust that may be overextended.
- Requires the session trend (last 12 bars) to be *opposite* the outsized move,
  so we are trading a pullback within a larger trend, not fighting it.
- Trades only during core hours (23:00-19:00 UTC) to avoid the noisy open/close.
- Uses a wider stop and target to give the give-back room to develop.

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
        trend_lookback: int = 12,
        range_mult: float = 1.8,
        min_typical_range: float = 0.5,
        size: int = 1,
        stop_offset: float = 2.0,
        target_offset: float = 3.0,
    ):
        self.interval = interval
        self.lookback = lookback
        self.trend_lookback = trend_lookback
        self.range_mult = range_mult
        self.min_typical_range = min_typical_range
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        self._session = None
        self._ranges: list[float] = []
        self._closes: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()
            self._closes.clear()

        history = ctx.history(self.interval)
        if not history:
            return
        last = history[-1]

        bar_range = last.high - last.low
        if bar_range > 0:
            self._ranges.append(bar_range)
            if len(self._ranges) > self.lookback:
                self._ranges.pop(0)

        self._closes.append(last.close)
        if len(self._closes) > self.trend_lookback:
            self._closes.pop(0)

        # Trade only during core hours: 23:00-19:00 UTC
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if not (23 * 60 <= minutes_since_midnight < 19 * 60):
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

        # Require a strong directional close: top or bottom quarter
        if not (close_position > 0.75 or close_position < 0.25):
            return

        # Session trend: sign of close change over trend_lookback bars
        trend = 0.0
        if len(self._closes) >= self.trend_lookback:
            trend = self._closes[-1] - self._closes[0]

        if ctx.position or ctx.working_order_ids:
            return

        # Give-back: outsized up bar, but session trend is down -> short
        if close_position > 0.75 and last.close > last.open and trend < 0:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-short",
            )
        # Outsized down bar, but session trend is up -> long
        elif close_position < 0.25 and last.close < last.open and trend > 0:
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
