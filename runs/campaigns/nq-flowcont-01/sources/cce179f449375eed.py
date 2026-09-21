"""
Order flow continuation with signal persistence and reduced overtrading.

Critical insight from trial #17 (score -0.8647): Exponential weighting and ratio-based
confirmation didn't improve over trial #1 (score -0.4823). The exponential weighting
may have introduced too much sensitivity to recent noise.

Key observation across all trials:
- Trial #1: score -0.4823, ~35k trades (best)
- Trial #16: score -0.8647, session filtering + adaptive sizing
- Trial #17: score -0.8289, exponential weighting + ratio confirmation
- Most other trials: scores worse than -0.75

The pattern is clear: Trial #1's simple approach (adaptive threshold + basic multi-timeframe)
was the best. Subsequent refinements made things worse.

New hypothesis: The issue is not signal quality but OVERTRADING. We're entering on every
signal that passes the filter, but many of these trades are marginal and lose to commission.

Approach for this trial:
1. Return to trial #1's core logic (adaptive volatility threshold + multi-timeframe)
2. Add trade frequency limiting: Only take trades if we've had at least N bars since
   the last exit. This reduces overtrading and lets mean reversion work against us less.
3. Increase the slow_imbalance requirement: Require it to exceed a minimum threshold
   (not just be positive). This filters out weak confirmations.
4. Track recent trades on self to avoid rapid re-entry after losses
5. Keep moderate brackets (stop_offset=0.8, target_offset=1.2) from trial #1
6. Use size=1 (trial #1 used size=2 but the smaller size may be more robust)
7. Slightly increase base_threshold to 0.14 (reduce entry frequency without killing signal)

The math: Trial #1 made ~35k trades. If we can reduce that to ~25-30k trades while
keeping the profitable ones, we reduce commission drag significantly while preserving
the edge. The key is filtering not by signal complexity but by trade frequency.

This is a conservative refinement: keep what worked (trial #1), reduce noise through
simpler means (trade frequency limiting, stricter slow confirmation), avoid over-engineering.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.14,
        volatility_window: int = 20,
        slow_threshold: float = 0.02,
        min_bars_between_trades: int = 3,
        size: int = 1,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Base imbalance threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            slow_threshold: Minimum slow imbalance (in addition to direction match)
            min_bars_between_trades: Minimum bars since last exit before next entry
            size: Position size
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.slow_threshold = slow_threshold
        self.min_bars_between_trades = min_bars_between_trades
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.bars_since_last_exit = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        # Track bars since last exit
        if ctx.position == 0:
            self.bars_since_last_exit += 1
        
        # Skip if we already have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Skip during warmup when orders would be ignored
        if not ctx.actionable:
            return

        # Skip if not enough bars have passed since last exit (reduce overtrading)
        if self.bars_since_last_exit < self.min_bars_between_trades:
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

        # Calculate adaptive threshold based on recent volatility
        volatility_lookback = min(self.volatility_window, len(fast_history))
        if volatility_lookback > 0:
            vol_window = fast_history.last(volatility_lookback)
            price_range = max(b.high for b in vol_window) - min(b.low for b in vol_window)
            avg_price = sum(b.close for b in vol_window) / len(vol_window)
            if avg_price > 0:
                volatility_ratio = price_range / avg_price
            else:
                volatility_ratio = 1.0
            # Adapt threshold: higher volatility = higher threshold needed
            threshold = self.base_threshold * (0.8 + 0.4 * volatility_ratio)
        else:
            threshold = self.base_threshold

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
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction AND exceed minimum threshold
        # - This filters out weak confirmations and reduces false signals
        # - Trade frequency limiting (bars_since_last_exit) reduces overtrading

        if fast_imbalance > threshold and slow_imbalance > self.slow_threshold:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self.bars_since_last_exit = 0
        elif fast_imbalance < -threshold and slow_imbalance < -self.slow_threshold:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )
            self.bars_since_last_exit = 0

    def on_fill(self, ctx) -> None:
        # Reset bars counter on any fill (entry or exit)
        self.bars_since_last_exit = 0

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
