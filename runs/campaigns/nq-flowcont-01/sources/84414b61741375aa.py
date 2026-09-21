"""
Order flow continuation with refined directional bias and tighter risk management.

Analysis of prior trials:
- Trial #0 (score -0.7544): Basic multi-timeframe, too many trades (8,925)
- Trial #1 (score -0.4823): Best result, adaptive volatility, 35,073 trades
- Trial #2 (score -0.9683): Session phase filtering hurt performance, 138,247 trades

The issue with #2: session phase filtering was too aggressive and eliminated
profitable periods. The asymmetric thresholds also seem to have backfired.

This trial refines #1 (the best performer) with:
1. Slightly lower base threshold to catch more flow signals (0.10 vs 0.12)
2. Reduce position size back to 1 (commission was eating into profits at size 2)
3. Tighter stops (0.6 vs 0.8) to reduce losses on false signals
4. More aggressive targets (1.5 vs 1.2) to reward when flow continues
5. Increase lookback periods slightly (6 vs 5 for fast) for more robust signals
6. Remove volatility multiplier complexity - use fixed threshold instead
7. Keep the multi-timeframe confirmation that worked in #1

The hypothesis: order flow continuation is real, but we need to:
- Trade less frequently (reduce size, tighter stops) to stay profitable after commission
- Keep the slow timeframe filter that worked
- Use simpler, more robust parameters
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 6,
        lookback_slow: int = 4,
        base_threshold: float = 0.10,
        size: int = 1,
        stop_offset: float = 0.6,
        target_offset: float = 1.5,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Imbalance threshold for entry
            size: Position size
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        # Skip if we already have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Skip during warmup when orders would be ignored
        if not ctx.actionable:
            return

        # Only act when the fast bar closes
        if self.fast_interval not in ctx.bars:
            return

        fast_history = ctx.history(self.fast_interval)
        slow_history = ctx.history(self.slow_interval)

        # Need enough history for both calculations
        if len(fast_history) < self.lookback_fast:
            return
        if len(slow_history) < self.lookback_slow:
            return

        # Get the fast timeframe lookback window for flow measurement
        fast_window = fast_history.last(self.lookback_fast)

        # Calculate signed order flow on fast timeframe
        total_delta = 0.0
        total_volume = 0.0

        for bar in fast_window:
            total_delta += bar.delta
            total_volume += bar.volume

        if total_volume <= 0:
            return

        # Normalize imbalance
        fast_imbalance = total_delta / total_volume

        # Get slow timeframe trend
        slow_window = slow_history.last(self.lookback_slow)
        slow_delta = 0.0
        slow_volume = 0.0

        for bar in slow_window:
            slow_delta += bar.delta
            slow_volume += bar.volume

        if slow_volume <= 0:
            return

        slow_imbalance = slow_delta / slow_volume

        # Entry logic:
        # - Fast imbalance must exceed threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - This filters out countertrend flow which is less reliable

        if fast_imbalance > self.base_threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -self.base_threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
