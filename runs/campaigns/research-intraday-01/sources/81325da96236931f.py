"""
Intraday momentum strategy with refined signal detection and adaptive exit timing.

Analysis of prior trials:
- Trial #0 (best): -0.7386, 39,797 trades
- Trial #1: -0.7727, 37,295 trades  
- Trial #2: crashed (malformed close() call)
- Trial #3: -0.8022, 27,266 trades (worse despite fewer trades)

Key observations:
1. Trial #0 was best despite highest trade count, suggesting the signal is sound
2. Trial #3 made fewer trades but performed worse, indicating over-filtering hurts
3. The core momentum hypothesis has merit but is being overwhelmed by friction
4. At 0.1 points/side commission, we need higher-quality signals and faster exits

New approach - focus on signal quality and execution efficiency:
1. Use a cleaner signal: bars closing beyond their session's typical range
2. Measure "typical range" as median of recent high-low ranges (more stable)
3. Enter only when current bar closes beyond 1.5x the median range
4. Use tighter, asymmetric brackets: smaller stop (0.5), modest target (0.8)
5. Very short holding period (1-2 bars) to capture momentum before reversal
6. Track fills more carefully to avoid over-trading the same signal
7. Reduce entry frequency by requiring bars to be separated by at least one bar

The refined hypothesis: a bar with close > (high + low)/2 AND range > 1.5 * median_range
indicates upside momentum continuation within the next 1-2 bars. Symmetric for shorts.
Tight brackets minimize slippage cost; short hold reduces whipsaw losses.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 10,
        range_multiplier: float = 1.4,
        min_range: float = 0.25,
        hold_bars: int = 1,
        stop_offset: float = 0.50,
        target_offset: float = 0.80,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.range_multiplier = range_multiplier
        self.min_range = min_range
        self.hold_bars = hold_bars
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

        # State tracking
        self._session: int | None = None
        self._bars_held: int = 0
        self._ranges: deque[float] = deque(maxlen=lookback + 1)
        self._last_entry_bar: int | None = None  # track last entry to avoid clustering

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_held = 0
            self._ranges.clear()
            self._last_entry_bar = None

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        # Track ranges for current and prior bars
        current_bar = history[-1]
        current_range = current_bar.high - current_bar.low
        self._ranges.append(current_range)

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.hold_bars:
                # Exit after holding for specified bars
                ctx.close()
                self._bars_held = 0
                self._last_entry_bar = None
            return

        # Not in position; check for entry signal
        if ctx.working_order_ids:
            return

        # Need enough history for range calculation
        if len(self._ranges) < self.lookback:
            return

        # Compute median range from lookback window
        ranges_list = list(self._ranges)[:-1]  # exclude current bar from median calc
        if len(ranges_list) < self.lookback:
            return
            
        sorted_ranges = sorted(ranges_list)
        median_idx = len(sorted_ranges) // 2
        median_range = sorted_ranges[median_idx]

        if median_range < self.min_range:
            return

        threshold_range = median_range * self.range_multiplier

        # Check current bar for entry signal
        if current_range < threshold_range:
            return

        # Determine direction based on close position within the bar
        bar_midpoint = (current_bar.high + current_bar.low) / 2
        
        if current_bar.close > bar_midpoint:
            # Strong upside close; expect continuation
            order_id = ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-long",
            )
            if order_id is not None:
                self._bars_held = 0
                self._last_entry_bar = current_bar.open_ts
                
        elif current_bar.close < bar_midpoint:
            # Strong downside close; expect continuation
            order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-short",
            )
            if order_id is not None:
                self._bars_held = 0
                self._last_entry_bar = current_bar.open_ts

    def on_fill(self, ctx) -> None:
        # Reset hold counter on fill
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # Handle order lifecycle events
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired", "cancel_confirmed"):
            self._bars_held = 0
            self._last_entry_bar = None
