"""
Order flow continuation with refined signal quality and reduced commission drag.

Key insight from trial #16 (score -0.8647): Session-phase filtering and adaptive
sizing didn't help. The core issue remains: we're trading too much relative to the
signal's true edge.

Analysis of best performer (trial #1, score -0.4823):
- ~35k trades, adaptive threshold + multi-timeframe confirmation
- The signal IS there, but commission (0.2 per round trip) dominates

Critical realization from trial progression:
- Trials with fewer trades (15-30k) scored worse than trial #1 (35k)
- Trials with more trades (50k+) scored much worse
- This suggests the optimal frequency is around 30-40k trades
- The problem: We need HIGHER QUALITY signals, not fewer signals

New approach - focus on signal QUALITY rather than frequency:
1. Keep trial #1's core (adaptive volatility threshold + multi-timeframe)
2. Refine the imbalance calculation: Use exponential weighting to emphasize recent bars
   - Recent bars matter more than older bars in the lookback window
   - This makes the signal more responsive to current flow conditions
3. Increase slow_interval lookback to 6 bars (more robust trend confirmation)
4. Fine-tune thresholds: base_threshold=0.13 (slightly higher than trial #1's implicit)
5. Use ratio-based confirmation: require slow_imbalance to be at least 40% of fast_imbalance
   - This is stricter than "same direction" but less strict than "both strong"
   - Filters noise without losing valid signals
6. Reduce position size back to 1 (trial #1 used size=2 but had worse results)
7. Adjust brackets: stop_offset=0.7, target_offset=1.0
   - Tighter stops reduce per-trade loss
   - Tighter targets match the actual edge size better

The math: If trial #1's edge was real at 35k trades despite commission,
then improving signal quality (exponential weighting + ratio confirmation)
should increase profitability while maintaining similar trade frequency.

This targets the core issue: We have a real signal, but it's buried in noise.
Better signal processing = better edge = better score.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 6,
        base_threshold: float = 0.13,
        volatility_window: int = 20,
        slow_confirmation_ratio: float = 0.40,
        size: int = 1,
        stop_offset: float = 0.7,
        target_offset: float = 1.0,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Base imbalance threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            slow_confirmation_ratio: Slow imbalance must be this fraction of fast (0.40 = 40%)
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
        self.slow_confirmation_ratio = slow_confirmation_ratio
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def _calculate_weighted_imbalance(self, bars: tuple) -> float:
        """
        Calculate imbalance with exponential weighting favoring recent bars.
        More recent bars have higher weight, making the signal more responsive.
        """
        if not bars or len(bars) == 0:
            return 0.0
        
        n = len(bars)
        # Exponential weights: most recent bar gets weight exp(1), oldest gets exp(1/n)
        weights = [2.718 ** (i / n) for i in range(1, n + 1)]
        total_weight = sum(weights)
        
        weighted_delta = 0.0
        weighted_volume = 0.0
        
        for i, bar in enumerate(bars):
            w = weights[i]
            weighted_delta += bar.delta * w
            weighted_volume += bar.volume * w
        
        if weighted_volume <= 0:
            return 0.0
        
        return weighted_delta / weighted_volume

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

        # Calculate weighted signed order flow on fast timeframe
        fast_imbalance = self._calculate_weighted_imbalance(fast_window)

        # Get slow timeframe trend
        slow_window = slow_history.last(self.lookback_slow)
        slow_imbalance = self._calculate_weighted_imbalance(slow_window)

        # Entry logic with refined confirmation:
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction AND be at least slow_confirmation_ratio
        #   of the fast imbalance magnitude (e.g., 40% of fast imbalance)
        # - This filters out countertrend noise while keeping valid signals
        # - Exponential weighting makes signal more responsive to current conditions

        if fast_imbalance > threshold:
            # Check if slow imbalance confirms (same direction and sufficient magnitude)
            if slow_imbalance > self.slow_confirmation_ratio * abs(fast_imbalance):
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="flow-long",
                )
        elif fast_imbalance < -threshold:
            # Check if slow imbalance confirms (same direction and sufficient magnitude)
            if slow_imbalance < -self.slow_confirmation_ratio * abs(fast_imbalance):
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
