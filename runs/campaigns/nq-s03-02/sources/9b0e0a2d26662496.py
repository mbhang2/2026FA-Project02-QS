"""Continuation after an outsized move, but only when the move is confirmed by
a second bar and the session's overall trend agrees.

The prior continuation trials (#3, #4, #11) lost money, and the give-back
trials (#5, #6, #7, #10) also lost money. This suggests that neither simple
continuation nor simple give-back after any outsized bar is profitable on this
dataset.

This strategy takes a more conservative approach: it trades continuation, but
requires three conditions to align:
1. An outsized bar (range > 1.8x typical) closes in the top/bottom quarter.
2. The next bar confirms by closing in the same direction (top/bottom half).
3. The session's trend, measured by the last 12 bars, agrees with the signal.

The session trend filter is the key addition. The first attempt's hourly
pattern showed that the effect, if any, was concentrated in specific hours and
months. By requiring the session trend to agree, we avoid fighting the
prevailing flow and only take continuation signals when the broader context
supports them.

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
        self._pending_signal: str | None = None  # "long" or "short"

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()
            self._closes.clear()
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

        self._closes.append(last.close)
        if len(self._closes) > self.trend_lookback:
            self._closes.pop(0)

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

        # Compute session trend: sign of close change over trend_lookback bars
        trend = 0.0
        if len(self._closes) >= self.trend_lookback:
            trend = self._closes[-1] - self._closes[0]

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

            if self._pending_signal == "long":
                if close_position > 0.5 and last.close > last.open and trend > 0:
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="outsized-confirmed-trend-long",
                    )
            elif self._pending_signal == "short":
                if close_position < 0.5 and last.close < last.open and trend < 0:
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="outsized-confirmed-trend-short",
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
