"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #11 continuation + one-bar confirmation: +0.0838
- #12 continuation + two-bar confirmation + trend: +0.0994
- #13 added volatility regime filter: NOT SCORED (insufficient exposure)
- #14 strong close + one confirmation: -0.0245
- #15 session trend filter: +0.2145 (best)
- #16 strong trend + min bars: +0.1965

#15 remains the best. #16's additional filters (min_bars, trend_strength)
reduced both score and trades, suggesting they may be filtering some
profitable signals. This variant returns to #15's simpler session trend
filter but adds a refinement: instead of using the session open price as
the trend reference, we use a short moving average of recent closes. This
makes the trend measure more responsive to the current market direction
while still being session-anchored.

Key changes from #15:
1. Replace session open price with a 10-bar simple moving average of closes.
2. Keep the two-bar confirmation with trend from #15 unchanged.
3. This should filter outsized moves that occur against the recent trend
   while allowing more signals than #16's stricter filters.

The hypothesis is that a moving average trend reference better captures
the "dominant direction" than the session open, which may be stale after
many bars.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar aligned with a moving average trend."""

    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 range_mult: float = 2.0, stop_mult: float = 1.0,
                 target_mult: float = 1.5, size: int = 1,
                 ma_length: int = 10):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.ma_length = ma_length
        self._session = None
        self._ranges: deque[float] = deque(maxlen=lookback)
        self._closes: deque[float] = deque(maxlen=ma_length)
        self._pending_direction: int = 0
        self._pending_range: float = 0.0
        self._confirmation_count: int = 0
        self._prev_bar = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()
            self._closes.clear()
            self._pending_direction = 0
            self._pending_range = 0.0
            self._confirmation_count = 0
            self._prev_bar = None

        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        rng = bar.high - bar.low
        if rng <= 0:
            return

        self._ranges.append(rng)
        self._closes.append(bar.close)

        # If we have a pending signal, check for two consecutive confirmation bars.
        if self._pending_direction != 0:
            if self._pending_direction == 1:
                if bar.close > bar.open and (
                    self._prev_bar is None or
                    (bar.high > self._prev_bar.high and bar.low > self._prev_bar.low)
                ):
                    self._confirmation_count += 1
                else:
                    self._pending_direction = 0
                    self._pending_range = 0.0
                    self._confirmation_count = 0
                    self._prev_bar = bar
                    return
            elif self._pending_direction == -1:
                if bar.close < bar.open and (
                    self._prev_bar is None or
                    (bar.high < self._prev_bar.high and bar.low < self._prev_bar.low)
                ):
                    self._confirmation_count += 1
                else:
                    self._pending_direction = 0
                    self._pending_range = 0.0
                    self._confirmation_count = 0
                    self._prev_bar = bar
                    return

            self._prev_bar = bar

            if self._confirmation_count >= 2:
                stop = self._pending_range * self.stop_mult
                target = self._pending_range * self.target_mult
                if self._pending_direction == 1:
                    ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                            tag="outsized-ma-trend-confirmed-long")
                else:
                    ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                             tag="outsized-ma-trend-confirmed-short")
                self._pending_direction = 0
                self._pending_range = 0.0
                self._confirmation_count = 0
                self._prev_bar = None
            return

        # No pending signal - check for outsized bar.
        if len(self._ranges) < self.lookback:
            return

        ordered = sorted(self._ranges)
        mid = len(ordered) // 2
        if len(ordered) % 2 == 0:
            median = (ordered[mid - 1] + ordered[mid]) / 2.0
        else:
            median = float(ordered[mid])
        if median <= 0:
            return

        if rng < median * self.range_mult:
            return

        if bar.close == bar.open:
            return

        # Need enough closes for the moving average.
        if len(self._closes) < self.ma_length:
            return

        ma = sum(self._closes) / len(self._closes)
        session_trend = 1 if bar.close > ma else -1

        if bar.close > bar.open:
            if session_trend != 1:
                return
            self._pending_direction = 1
        else:
            if session_trend != -1:
                return
            self._pending_direction = -1

        self._pending_range = rng
        self._confirmation_count = 0
        self._prev_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
