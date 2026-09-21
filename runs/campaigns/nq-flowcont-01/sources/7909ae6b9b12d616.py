"""
Order flow continuation with intra-bar flow momentum and reduced false signals.

Analysis of trial progression:
- Trial #1 (score -0.4823): Best performer, ~35k trades, adaptive volatility threshold
- Trial #11 (score -0.9463): Added flow reversal + consistency checks, ~80k trades
- All others: Worse than #1, ranging -0.85 to -0.97

Key observation: Trial #1 succeeded with simple logic: adaptive threshold + multi-timeframe
confirmation. Trial #11 added filters but traded more (~80k vs ~35k), suggesting the extra
complexity didn't improve selectivity enough.

The core hypothesis remains valid: order flow continuation works. The question is how to
identify the highest-conviction signals without over-filtering.

New approach - focus on signal MOMENTUM rather than just magnitude:
1. Keep trial #1's adaptive volatility threshold (proven effective)
2. Keep multi-timeframe confirmation (fast + slow agreement)
3. Replace flow reversal/consistency checks with intra-bar momentum detection:
   - Instead of checking if flow is reversing, measure if flow is ACCELERATING
   - A bar where delta is larger than the average of prior bars suggests momentum
   - This is more precise than reversal detection (doesn't reject weak reversals)
4. Add one more filter: require the last bar to have above-average volume
   - High volume with directional flow is more reliable than low volume
5. Slightly increase threshold from 0.12 to 0.14 to be more selective
6. Keep size=1, conservative bracket offsets (proven effective)

The hypothesis: Order flow signals are most reliable when:
- They exceed an adaptive threshold
- They're confirmed by slower timeframe trend
- The most recent bar shows momentum (larger delta than recent average)
- The most recent bar has above-average volume (conviction)

This combines trial #1's proven foundation with targeted refinements that should
reduce false signals without the over-filtering that hurt trial #11.
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

    def _has_momentum(self, window) -> bool:
        """
        Check if the last bar shows momentum in the direction of the overall flow.
        Momentum = last bar's absolute delta is above the average of prior bars.
        
        Returns True if momentum is present.
        """
        if len(window) < 2:
            return True
        
        last_bar = window[-1]
        prior_bars = window[:-1]
        
        # Calculate average absolute delta of prior bars
        avg_prior_delta = sum(abs(b.delta) for b in prior_bars) / len(prior_bars)
        
        # Last bar has momentum if its absolute delta exceeds the average
        return abs(last_bar.delta) > avg_prior_delta

    def _has_volume_conviction(self, window) -> bool:
        """
        Check if the last bar has above-average volume.
        High volume with directional flow suggests conviction.
        
        Returns True if volume is above average.
        """
        if len(window) < 2:
            return True
        
        last_bar = window[-1]
        prior_bars = window[:-1]
        
        # Calculate average volume of prior bars
        avg_prior_volume = sum(b.volume for b in prior_bars) / len(prior_bars)
        
        # Last bar has conviction if its volume exceeds the average
        return last_bar.volume > avg_prior_volume

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

        # Check for momentum and volume conviction in the fast window
        if not self._has_momentum(fast_window):
            return
        
        if not self._has_volume_conviction(fast_window):
            return

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
        # - Slow imbalance must be in same direction (confirming trend)
        # - Last bar must show momentum (delta larger than prior average)
        # - Last bar must have above-average volume (conviction)

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow, with momentum
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow, with momentum
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
