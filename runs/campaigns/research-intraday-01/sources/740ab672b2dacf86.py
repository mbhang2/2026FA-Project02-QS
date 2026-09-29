"""
Intraday momentum strategy with volatility-normalized signals and regime filtering.

Analysis of prior trials:
- Trial #0 (best): -0.7386, 39,797 trades
- Trial #1: -0.7727, 37,295 trades
- Trial #2: crashed (malformed close() call)
- Trial #3: -0.8022, 27,266 trades
- Trial #4: -0.7523, 18,966 trades

Key insights:
1. Trial #0 had the best score with highest trade count, suggesting the signal works
2. Reducing trades (trials #3, #4) made performance worse, not better
3. The momentum hypothesis is real but being overwhelmed by 0.1 point/side commission
4. We need to:
   - Increase signal quality to reduce false entries (lower trade count from #0's 39k)
   - Keep the core momentum insight that was working in #0
   - Use volatility normalization to adapt to changing market conditions
   - Tighten execution to minimize slippage on both entry and exit

New approach:
1. Normalize entry signals by recent volatility (ATR-like measure)
2. Enter only on bars with exceptional momentum relative to that session's volatility
3. Use asymmetric brackets: tighter stop (0.4 points) to cut losses, modest target (0.9)
4. Hold for 2-3 bars to capture the continuation move but exit before reversal
5. Track the session's volatility regime to avoid low-quality signals
6. Require the momentum bar to close decisively (>60% of its range) in the direction

The refined hypothesis: a bar that (1) has range in top 40% of recent session ranges,
(2) closes decisively (>60% of range) in one direction, and (3) shows strong close-to-close
momentum from prior bar, indicates continuation within 2-3 bars. This filters #0's 39k
trades to higher-conviction signals while keeping the winning core logic.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 12,
        range_percentile: float = 0.40,
        close_strength: float = 0.60,
        hold_bars: int = 2,
        stop_offset: float = 0.40,
        target_offset: float = 0.90,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.range_percentile = range_percentile
        self.close_strength = close_strength
        self.hold_bars = hold_bars
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

        # State tracking across callbacks
        self._session: int | None = None
        self._bars_held: int = 0
        self._ranges: deque[float] = deque(maxlen=lookback + 1)
        self._closes: deque[float] = deque(maxlen=lookback + 2)

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
            self._closes.clear()

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current_bar = history[-1]
        current_range = current_bar.high - current_bar.low
        self._ranges.append(current_range)
        self._closes.append(current_bar.close)

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.hold_bars:
                ctx.close()
                self._bars_held = 0
            return

        # Not in position; check for entry
        if ctx.working_order_ids:
            return

        # Need sufficient history
        if len(self._ranges) < self.lookback or len(self._closes) < 2:
            return

        # Compute range threshold (top percentile = stronger signals)
        ranges_list = list(self._ranges)
        sorted_ranges = sorted(ranges_list)
        percentile_idx = max(0, int(len(sorted_ranges) * self.range_percentile))
        threshold_range = sorted_ranges[percentile_idx]

        # Skip if current bar's range is not exceptional
        if current_range < threshold_range:
            return

        # Check if bar closes decisively in one direction
        range_from_low = current_bar.close - current_bar.low
        range_from_high = current_bar.high - current_bar.close
        
        if current_range > 0:
            close_position_ratio = range_from_low / current_range
        else:
            return

        # Strong upside close: close is in upper 60% of the range
        if close_position_ratio > self.close_strength:
            # Confirm with momentum: prior bar also closed up
            prev_bar = history[-2]
            if prev_bar.close > prev_bar.open:
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="momentum-long",
                )
                if order_id is not None:
                    self._bars_held = 0

        # Strong downside close: close is in lower 40% of the range
        elif close_position_ratio < (1.0 - self.close_strength):
            # Confirm with momentum: prior bar also closed down
            prev_bar = history[-2]
            if prev_bar.close < prev_bar.open:
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="momentum-short",
                )
                if order_id is not None:
                    self._bars_held = 0

    def on_fill(self, ctx) -> None:
        # Reset hold counter on fill
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # Handle order lifecycle events
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired", "cancel_confirmed"):
            self._bars_held = 0
