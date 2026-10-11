"""Outsized-move continuation with trend filter and session-time restriction.

The prior continuation trials (#3, #4) lost money, but the give-back trials
(#5, #6, #7) also lost money. This suggests the effect may not be a simple
continuation or give-back after any outsized bar, but may depend on the
broader trend context.

This strategy keeps the continuation claim but adds a trend filter: it only
trades in the direction of the session's overall trend, measured by comparing
the current price to a longer moving average. This should reduce the number of
trades and avoid fighting the dominant flow.

Key changes from #4:
1. Add a 20-bar simple moving average of closes as a trend filter.
2. Only take long continuation signals if price is above the SMA, and short
   signals if price is below the SMA.
3. Restrict trading to the first 4 hours of the session (22:00-02:00 UTC),
   where the original campaign results showed the strongest continuation
   effect (hours 19 and 22 in the first attempt's hourly pattern).
4. Slightly wider stop and target to give the trend-following trades more room.

All state is kept on self. The SMA is computed incrementally from the history
window, which is bounded at 512 bars, so no WindowTooShort crash can occur.
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
        trend_lookback: int = 20,
        size: int = 1,
        stop_offset: float = 2.5,
        target_offset: float = 4.0,
    ):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.min_typical_range = min_typical_range
        self.trend_lookback = trend_lookback
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

        # Restrict to the first 4 hours of the session (22:00-02:00 UTC)
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if not (22 * 60 <= minutes_since_midnight < 24 * 60 or
                0 <= minutes_since_midnight < 2 * 60):
            return

        if len(self._ranges) < self.lookback:
            return

        typical_range = sum(self._ranges) / len(self._ranges)
        if typical_range <= 0 or typical_range < self.min_typical_range:
            return

        if bar_range < self.range_mult * typical_range:
            return

        # Trend filter: need enough closes for the SMA
        if len(self._closes) < self.trend_lookback:
            return
        sma = sum(self._closes) / len(self._closes)

        # Confirmation: close must be in the top or bottom quarter of the bar
        bar_span = last.high - last.low
        if bar_span <= 0:
            return
        close_position = (last.close - last.low) / bar_span

        if ctx.position or ctx.working_order_ids:
            return

        if close_position > 0.75 and last.close > last.open and last.close > sma:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-continuation-trend-long",
            )
        elif close_position < 0.25 and last.close < last.open and last.close < sma:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-continuation-trend-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
