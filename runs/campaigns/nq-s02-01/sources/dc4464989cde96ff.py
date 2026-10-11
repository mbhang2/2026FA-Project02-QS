"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #11 continuation + one-bar confirmation: +0.0838
- #12 continuation + two-bar confirmation + trend: +0.0994
- #15 session trend filter: +0.2145 (best)
- #16 strong trend + min bars: +0.1965
- #17 moving average trend: +0.0979
- #18 volume filter: +0.2248 (best)

#18 added a volume filter to #15 and improved the score. This variant
builds on #18 but changes the confirmation logic. Instead of requiring
two consecutive bars in the direction of the outsized move, we require
the outsized bar itself to close near its extreme (strong close) and
then only one confirmation bar. This should capture continuation earlier,
before the move gives back.

Key changes from #18:
1. Add a strong-close filter on the outsized bar: close must be in the
   top/bottom 20% of its range.
2. Reduce confirmation requirement from two bars to one.
3. Keep session open trend filter and volume filter unchanged.

The hypothesis is that a strong close on the outsized bar indicates
conviction, allowing earlier entry with one fewer confirmation bar.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized, high-volume bar with a strong close."""

    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 range_mult: float = 2.0, stop_mult: float = 1.0,
                 target_mult: float = 1.5, size: int = 1,
                 volume_mult: float = 1.5, strong_close_frac: float = 0.2):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.volume_mult = volume_mult
        self.strong_close_frac = strong_close_frac
        self._session = None
        self._session_open_price: float | None = None
        self._ranges: deque[float] = deque(maxlen=lookback)
        self._volumes: deque[float] = deque(maxlen=lookback)
        self._pending_direction: int = 0
        self._pending_range: float = 0.0
        self._prev_bar = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._session_open_price = None
            self._ranges.clear()
            self._volumes.clear()
            self._pending_direction = 0
            self._pending_range = 0.0
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

        if self._session_open_price is None:
            self._session_open_price = bar.open

        self._ranges.append(rng)
        self._volumes.append(float(bar.volume))

        # Pending signal: wait for one confirmation bar.
        if self._pending_direction != 0:
            if self._pending_direction == 1:
                if bar.close > bar.open and (
                    self._prev_bar is None or
                    (bar.high > self._prev_bar.high and bar.low > self._prev_bar.low)
                ):
                    stop = self._pending_range * self.stop_mult
                    target = self._pending_range * self.target_mult
                    ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                            tag="outsized-strong-close-confirmed-long")
                self._pending_direction = 0
                self._pending_range = 0.0
                self._prev_bar = None
            elif self._pending_direction == -1:
                if bar.close < bar.open and (
                    self._prev_bar is None or
                    (bar.high < self._prev_bar.high and bar.low < self._prev_bar.low)
                ):
                    stop = self._pending_range * self.stop_mult
                    target = self._pending_range * self.target_mult
                    ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                             tag="outsized-strong-close-confirmed-short")
                self._pending_direction = 0
                self._pending_range = 0.0
                self._prev_bar = None
            return

        # No pending signal - check for outsized bar.
        if len(self._ranges) < self.lookback:
            return

        ordered_ranges = sorted(self._ranges)
        mid = len(ordered_ranges) // 2
        if len(ordered_ranges) % 2 == 0:
            median_range = (ordered_ranges[mid - 1] + ordered_ranges[mid]) / 2.0
        else:
            median_range = float(ordered_ranges[mid])
        if median_range <= 0:
            return

        if rng < median_range * self.range_mult:
            return

        # Volume filter: outsized bar must have above-average volume.
        ordered_volumes = sorted(self._volumes)
        mid_v = len(ordered_volumes) // 2
        if len(ordered_volumes) % 2 == 0:
            median_volume = (ordered_volumes[mid_v - 1] + ordered_volumes[mid_v]) / 2.0
        else:
            median_volume = float(ordered_volumes[mid_v])
        if median_volume <= 0:
            return
        if float(bar.volume) < median_volume * self.volume_mult:
            return

        if bar.close == bar.open:
            return

        # Strong close filter: close must be near the extreme in the direction.
        if bar.close > bar.open:
            strong_close = bar.close >= bar.high - self.strong_close_frac * rng
        else:
            strong_close = bar.close <= bar.low + self.strong_close_frac * rng
        if not strong_close:
            return

        if self._session_open_price is None:
            return
        session_trend = 1 if bar.close > self._session_open_price else -1

        if bar.close > bar.open:
            if session_trend != 1:
                return
            self._pending_direction = 1
        else:
            if session_trend != -1:
                return
            self._pending_direction = -1

        self._pending_range = rng
        self._prev_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
