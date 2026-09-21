"""
Order flow continuation with refined entry signals and reduced false positives.

Analysis of prior trials shows a pattern:
- Trial #1 (score -0.4823): Best result with adaptive volatility and multi-timeframe
- Trial #3 (score -0.9560): Simplified version performed worse
- Trial #2 (score -0.9683): Session filtering was too aggressive

The core insight: the adaptive volatility-based threshold in #1 was working well.
The issue may be that we're trading too frequently on weak signals.

This trial refines the approach:
1. Restore adaptive volatility threshold from #1 (it was the best performer)
2. Add a minimum volume filter to avoid trading on thin bars
3. Increase the slow timeframe lookback slightly (5 vs 4) for stronger confirmation
4. Use a stricter slow imbalance threshold (require |slow_imbalance| > 0.02)
5. Add a check that fast and slow imbalances are strongly aligned (not just same sign)
6. Keep size at 1 and bracket parameters from #3 (tighter stops, better rewards)
7. Reduce false positives by requiring higher quality flow signals

The hypothesis: order flow continuation works, but we need to be more selective
about which signals we trade. Filter for:
- Bars with sufficient volume (not thin/illiquid)
- Stronger slow-timeframe confirmation
- Better alignment between fast and slow flow
- This should reduce the trade count while improving quality
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 5,
        base_threshold: float = 0.12,
        volatility_window: int = 20,
        slow_threshold: float = 0.02,
        min_volume_percentile: float = 0.3,
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
            base_threshold: Base imbalance threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            slow_threshold: Minimum absolute slow imbalance to confirm trend
            min_volume_percentile: Skip bars with volume below this percentile
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
        self.min_volume_percentile = min_volume_percentile
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

        # Check that current bar has sufficient volume
        current_bar = fast_history[-1]
        if len(fast_history) >= 20:
            recent_volumes = [b.volume for b in fast_history.last(20)]
            volume_threshold = sorted(recent_volumes)[int(len(recent_volumes) * self.min_volume_percentile)]
            if current_bar.volume < volume_threshold:
                return

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

        # Get slow timeframe trend with stricter confirmation
        slow_window = slow_history.last(self.lookback_slow)
        slow_delta = 0.0
        slow_volume = 0.0

        for bar in slow_window:
            slow_delta += bar.delta
            slow_volume += bar.volume

        if slow_volume <= 0:
            return

        slow_imbalance = slow_delta / slow_volume

        # Entry logic with stricter filters:
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction AND above minimum threshold
        # - This ensures strong confirmation, not just same sign

        if fast_imbalance > threshold and slow_imbalance > self.slow_threshold:
            # Strong buying pressure on fast, strongly confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < -self.slow_threshold:
            # Strong selling pressure on fast, strongly confirmed by slow
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
