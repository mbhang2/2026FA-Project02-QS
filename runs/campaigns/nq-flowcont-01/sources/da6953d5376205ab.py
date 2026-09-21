"""
Order flow continuation with momentum confirmation and reduced whipsaws.

Analysis of prior trials:
- Trial #1 (score -0.4823): Best result with adaptive volatility
- Trial #4 (score -0.9197): Added volume filter and stricter slow threshold, hurt performance
- Trial #3 (score -0.9560): Simplified version performed worse

The pattern suggests:
1. Adaptive volatility threshold works well (from #1)
2. Multi-timeframe confirmation is valuable
3. But we may be over-filtering (trial #4's stricter filters hurt)
4. The slow timeframe confirmation in #1 was working - it was simple (just same sign)

New approach - focus on momentum quality:
1. Keep the adaptive volatility threshold from #1 (it was the best performer)
2. Instead of stricter slow imbalance thresholds, use recent momentum confirmation
3. Check that the last few bars on the fast timeframe show continuation (not reversal)
4. Add a simple momentum filter: require the last bar's delta to be in the same direction
5. This reduces whipsaws without over-filtering
6. Keep multi-timeframe confirmation but make it simpler and more robust
7. Use slightly higher volatility adjustment range to be more adaptive

The hypothesis: order flow continuation works best when:
- There's a strong flow signal on the fast timeframe
- Recent momentum confirms the direction (not just aggregate flow)
- The slow timeframe shows general agreement
- We don't over-filter with arbitrary thresholds

This balances signal quality with trade frequency better than trial #4.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.12,
        volatility_window: int = 20,
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
        if len(fast_history) < self.lookback_fast + 1:  # +1 for momentum check
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

        # Check recent momentum: the last bar should show continuation
        # This filters out cases where flow is building but momentum is reversing
        last_bar = fast_history[-1]
        
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
        # - Last bar's delta must be in same direction (momentum confirmation)
        # - Slow imbalance must be in same direction (trend confirmation)
        # This is simpler than trial #4 but still filters false signals

        if fast_imbalance > threshold and last_bar.delta > 0 and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by last bar momentum and slow trend
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and last_bar.delta < 0 and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by last bar momentum and slow trend
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
