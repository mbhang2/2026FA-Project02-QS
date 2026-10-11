"""Give-back with adaptive threshold, delayed entry, and session context.

After an outsized bar, wait for a pullback bar that closes back inside the
prior bar's range, then enter opposite with a bracket. The threshold adapts to
the session's own bar ranges, and the session must have moved enough to make
the give-back meaningful. The entry is delayed by one bar after the outsized
bar to avoid entering while the move is still extending.

Changes from #13:
- Increase the minimum session move required to trigger a signal, so we only
  trade after a genuinely large move relative to the session's own volatility.
- Use a fixed multiple of the rolling median rather than a percentile of all
  ranges, which is more stable and less sensitive to outliers.
- Tighten the stop and widen the target slightly, since give-back moves are
  often smaller than the initial thrust but can extend.
- Require a larger number of bars before trading to establish a more reliable
  baseline.
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
        stop_mult: float = 0.5,
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
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None
        self._pending_outsized = False
        self._pending_direction: Optional[str] = None  # "short" or "long"

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
        # close inside the prior range.
        if self._pending_outsized and self._pending_direction is not None:
            if prev_high is None or prev_low is None:
                self._pending_outsized = False
                self._pending_direction = None
                return

            # Pullback confirmation: this bar closes back inside the prior
            # bar's range.
            if prev_low <= bar.close <= prev_high:
                stop_offset = max(bar_range * self.stop_mult, 0.25)
                target_offset = max(bar_range * self.target_mult, 0.25)

                if self._pending_direction == "short":
                    ctx.sell(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="giveback-short",
                    )
                else:
                    ctx.buy(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="giveback-long",
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
        direction = "short" if bar.close > bar.open else "long"
        self._pending_outsized = True
        self._pending_direction = direction

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
