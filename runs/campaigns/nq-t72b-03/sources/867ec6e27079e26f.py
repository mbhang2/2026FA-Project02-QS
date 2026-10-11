"""Continuation after outsized bar with tighter confirmation.

This strategy tests the alternative hypothesis: an outsized bar continues
rather than gives back. After an outsized bar, wait for the next bar to close
in the same direction as the outsized bar, then enter with that direction.
Uses rolling median of recent ranges as the baseline, requires a meaningful
session move, and uses a bracket with a relatively tight stop and wider target
to let continuation runs play out.

Changes from #14:
- Flip the direction: enter in the direction of the outsized bar after a
  confirmation bar closes in the same direction, rather than fading it.
- Keep the adaptive threshold and session context.
- Slightly wider stop to avoid being shaken out during continuation, but
  still smaller than target.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 15,
        range_lookback: int = 20,
        range_mult: float = 2.0,
        stop_mult: float = 0.7,
        target_mult: float = 1.8,
        size: int = 1,
        min_time_left_ns: int = 45 * 60 * 1_000_000_000,
        session_open_skip_ns: int = 20 * 60 * 1_000_000_000,
        session_move_mult: float = 2.0,
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.range_lookback = range_lookback
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_time_left_ns = min_time_left_ns
        self.session_open_skip_ns = session_open_skip_ns
        self.session_move_mult = session_move_mult

        self._session: Optional[int] = None
        self._bars_this_session = 0
        self._ranges: deque[float] = deque(maxlen=range_lookback)
        self._session_open_price: Optional[float] = None
        self._pending_outsized = False
        self._pending_direction: Optional[str] = None  # "long" or "short"

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._ranges.clear()
            self._session_open_price = None
            self._pending_outsized = False
            self._pending_direction = None

        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        if self._session_open_price is None:
            self._session_open_price = bar.open

        self._bars_this_session += 1
        bar_range = bar.high - bar.low
        self._ranges.append(bar_range)

        if not ctx.actionable:
            return

        if ctx.ts - ctx.session_start < self.session_open_skip_ns:
            return

        if self._bars_this_session < self.min_bars:
            return

        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            return

        if ctx.position or ctx.working_order_ids:
            return

        if bar.close == bar.open:
            return

        # If we have a pending outsized bar, check this bar for continuation.
        if self._pending_outsized and self._pending_direction is not None:
            # Continuation confirmation: this bar closes in the same direction
            # as the outsized bar.
            if self._pending_direction == "long" and bar.close > bar.open:
                stop_offset = max(bar_range * self.stop_mult, 0.25)
                target_offset = max(bar_range * self.target_mult, 0.25)
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="continuation-long",
                )
            elif self._pending_direction == "short" and bar.close < bar.open:
                stop_offset = max(bar_range * self.stop_mult, 0.25)
                target_offset = max(bar_range * self.target_mult, 0.25)
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="continuation-short",
                )

            self._pending_outsized = False
            self._pending_direction = None
            return

        # Check for an outsized bar using the rolling median.
        if len(self._ranges) < self.range_lookback:
            return

        sorted_ranges = sorted(self._ranges)
        mid = len(sorted_ranges) // 2
        if len(sorted_ranges) % 2 == 0:
            median = (sorted_ranges[mid - 1] + sorted_ranges[mid]) / 2.0
        else:
            median = sorted_ranges[mid]
        threshold = median * self.range_mult
        if threshold <= 0:
            return

        if bar_range < threshold:
            return

        session_move = abs(bar.close - self._session_open_price)
        if session_move < self.session_move_mult * median:
            return

        # Mark as pending and wait for the next bar.
        direction = "long" if bar.close > bar.open else "short"
        self._pending_outsized = True
        self._pending_direction = direction

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
