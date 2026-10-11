"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #11 continuation + one-bar confirmation: +0.0838
- #12 continuation + two-bar confirmation + trend: +0.0994
- #15 session trend filter: +0.2145 (best)
- #16 strong trend + min bars: +0.1965
- #17 moving average trend: +0.0979

#15 remains the best. #17's moving average trend reference hurt performance
significantly compared to the session open price. This suggests the session
open is a more stable anchor for the trend, and that recent closes may be
too noisy. This variant builds on #15 but adds a volume filter: outsized
bars are more likely to continue if they occur on above-average volume,
indicating genuine participation rather than a thin, easily-reversed move.

Key changes from #15:
1. Track session volume per bar.
2. Require the outsized bar's volume to be above a multiple of the session's
   median bar volume (e.g., 1.5x).
3. Keep the session open trend filter and two-bar confirmation unchanged.

The hypothesis is that volume confirms the conviction behind an outsized
move, filtering out low-participation spikes that tend to give back.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized, high-volume bar aligned with session trend."""

    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 range_mult: float = 2.0, stop_mult: float = 1.0,
                 target_mult: float = 1.5, size: int = 1,
                 volume_mult: float = 1.5):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.volume_mult = volume_mult
        self._session = None
        self._session_open_price: float | None = None
        self._ranges: deque[float] = deque(maxlen=lookback)
        self._volumes: deque[float] = deque(maxlen=lookback)
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
            self._session_open_price = None
            self._ranges.clear()
            self._volumes.clear()
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

        if self._session_open_price is None:
            self._session_open_price = bar.open

        self._ranges.append(rng)
        self._volumes.append(float(bar.volume))

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
                            tag="outsized-volume-trend-confirmed-long")
                else:
                    ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                             tag="outsized-volume-trend-confirmed-short")
                self._pending_direction = 0
                self._pending_range = 0.0
                self._confirmation_count = 0
                self._prev_bar = None
            return

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
        self._confirmation_count = 0
        self._prev_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
