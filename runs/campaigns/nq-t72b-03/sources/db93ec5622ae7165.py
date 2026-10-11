"""Continuation after outsized bar with pullback entry and session context.

This strategy explores the opposite hypothesis from the give-back family:
after an outsized bar, price continues in the same direction. We wait for a
small pullback that does not fully retrace the outsized bar, then enter in the
direction of the move. The threshold adapts to the session's own bar ranges,
and the session must have moved enough to make the continuation meaningful.

Changes from prior give-back strategies:
- Direction is flipped: we trade with the outsized move, not against it.
- Pullback confirmation requires the bar to close within the prior bar's range
  but still beyond the midpoint in the direction of the move.
- Use a smaller range multiplier to catch more moderate outsized bars.
- Keep the stop tight and target moderate, as continuation moves can be
  persistent but often encounter resistance.
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
        range_mult: float = 1.8,
        stop_mult: float = 0.6,
        target_mult: float = 1.5,
        size: int = 1,
        min_time_left_ns: int = 45 * 60 * 1_000_000_000,
        session_open_skip_ns: int = 20 * 60 * 1_000_000_000,
        session_move_mult: float = 1.5,
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
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None
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
            self._prev_high = None
            self._prev_low = None
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

        prev_high = self._prev_high
        prev_low = self._prev_low
        self._prev_high = bar.high
        self._prev_low = bar.low

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

        # If we have a pending outsized bar, check this bar for a pullback
        # that stays in the direction of the move.
        if self._pending_outsized and self._pending_direction is not None:
            if prev_high is None or prev_low is None:
                self._pending_outsized = False
                self._pending_direction = None
                return

            midpoint = (prev_high + prev_low) / 2.0

            # For a long continuation (after an up move), the pullback bar
            # must close above the midpoint of the prior bar's range.
            # For a short continuation (after a down move), it must close
            # below the midpoint.
            if self._pending_direction == "long" and bar.close > midpoint:
                stop_offset = max(bar_range * self.stop_mult, 0.25)
                target_offset = max(bar_range * self.target_mult, 0.25)
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="continuation-long",
                )
            elif self._pending_direction == "short" and bar.close < midpoint:
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
