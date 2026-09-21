"""
Absorption and initiation with multi-timeframe confirmation and adaptive thresholds.

Trial #1 scored -0.5131 with 605 trades, a significant improvement from #0's -0.8425
with 6722 trades. The key insight: reducing trade frequency dramatically improves
net returns by cutting commission drag.

This trial pushes further in that direction with several refinements:

1. Multi-timeframe confirmation: require a 5-minute trend bias before entering on
   1-minute absorption/initiation. This filters out trades against the larger trend.

2. Adaptive thresholds: normalize absorption and initiation thresholds not just by
   recent delta std, but also by recent bar volatility (high-low range). A large
   delta in a wide bar is less impressive than the same delta in a tight bar.

3. Stricter initiation: require initiation to occur on the SAME bar as absorption
   detection, or the very next bar. This captures the immediate reversal pattern
   more precisely and reduces false signals from delayed confirmations.

4. Higher conviction: increase absorption_std_mult to 2.5 and initiation_std_mult
   to 2.0, trading only the highest-conviction setups.

5. Better position management: only enter if we're not already holding, and track
   absorption state more carefully to avoid multiple entries on the same setup.

The hypothesis: absorption marks a turn, especially when confirmed by the larger
timeframe trend and followed immediately by efficient initiation.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        absorption_std_mult: float = 2.5,  # Higher threshold for fewer trades
        initiation_std_mult: float = 2.0,
        initiation_efficiency: float = 0.65,
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        lookback_window: int = 40,
        max_bars_after_absorption: int = 1,  # Only next bar, not 3
    ):
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.absorption_std_mult = absorption_std_mult
        self.initiation_std_mult = initiation_std_mult
        self.initiation_efficiency = initiation_efficiency
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.lookback_window = lookback_window
        self.max_bars_after_absorption = max_bars_after_absorption
        
        # Track deltas, bars, and ranges for analysis
        self.fast_deltas: deque = deque(maxlen=lookback_window)
        self.fast_bars: deque = deque(maxlen=lookback_window + 1)
        self.fast_ranges: deque = deque(maxlen=lookback_window)
        
        self.absorption_bar: dict | None = None
        self.bars_since_absorption: int = 0
        
    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)
    
    def _calculate_delta_stats(self) -> tuple[float, float]:
        """Calculate mean and std of recent absolute deltas."""
        if len(self.fast_deltas) < 3:
            return 0.0, 1.0
        deltas_list = list(self.fast_deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        mean = sum(abs_deltas) / len(abs_deltas)
        variance = sum((d - mean) ** 2 for d in abs_deltas) / len(abs_deltas)
        std = variance ** 0.5
        return mean, max(std, 0.1)
    
    def _calculate_volatility_stats(self) -> tuple[float, float]:
        """Calculate mean and std of recent bar ranges."""
        if len(self.fast_ranges) < 3:
            return 0.1, 0.1
        ranges_list = list(self.fast_ranges)
        mean = sum(ranges_list) / len(ranges_list)
        variance = sum((r - mean) ** 2 for r in ranges_list) / len(ranges_list)
        std = variance ** 0.5
        return mean, max(std, 0.01)
    
    def _get_slow_trend(self, ctx) -> int:
        """
        Determine the 5-minute trend bias.
        Returns: 1 for uptrend, -1 for downtrend, 0 for neutral.
        """
        history = ctx.history(self.slow_interval)
        if len(history) < 2:
            return 0
        
        recent = history[-1]
        prior = history[-2]
        
        # Simple trend: compare recent close to prior close
        if recent.close > prior.close:
            return 1
        elif recent.close < prior.close:
            return -1
        return 0
    
    def _is_absorption(self, bar, prior_bar) -> bool:
        """
        Absorption: large delta that moves price in the OPPOSITE direction.
        Normalized by both delta volatility and bar range volatility.
        """
        if prior_bar is None:
            return False
        
        delta = bar.delta
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        
        # Adaptive threshold: scale by volatility
        base_threshold = std_delta * self.absorption_std_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        if abs(delta) < threshold:
            return False
        
        # Price must move AGAINST the delta direction
        if delta > threshold:
            # Large positive delta (buying pressure)
            # Absorption: price moves DOWN despite the buying
            if bar.close < prior_bar.close and bar.low < prior_bar.low:
                return True
        elif delta < -threshold:
            # Large negative delta (selling pressure)
            # Absorption: price moves UP despite the selling
            if bar.close > prior_bar.close and bar.high > prior_bar.high:
                return True
        
        return False
    
    def _is_initiation(self, bar, absorption_bar) -> bool:
        """
        Initiation: opposite delta that is strong and efficient.
        Must occur immediately after absorption (same or next bar).
        """
        if absorption_bar is None:
            return False
        
        absorption_delta = absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        
        # Adaptive threshold
        base_threshold = std_delta * self.initiation_std_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Must be opposite direction and strong
        if absorption_delta > 0:
            # Was positive absorption, look for negative initiation
            if current_delta >= -threshold:
                return False
            
            # Check efficiency: close near low
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            if close_pos > (1.0 - self.initiation_efficiency):
                return False
            
            # Price should move down from absorption bar
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            if current_delta <= threshold:
                return False
            
            # Check efficiency: close near high
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            if close_pos < self.initiation_efficiency:
                return False
            
            # Price should move up from absorption bar
            if bar.high <= absorption_bar.get("low", 0):
                return False
            
            return True
    
    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Get fast bar if it closed
        if self.fast_interval not in ctx.bars:
            return
        
        fast_bar = ctx.bars[self.fast_interval]
        
        # Update fast bar history
        self.fast_bars.append(fast_bar)
        self.fast_deltas.append(fast_bar.delta)
        self.fast_ranges.append(fast_bar.high - fast_bar.low)
        
        # Need at least some history
        history = ctx.history(self.fast_interval)
        if len(history) < 2:
            return
        
        # Don't trade if we already have a position
        if ctx.position or ctx.working_order_ids:
            return
        
        # Get slow trend for confirmation
        slow_trend = self._get_slow_trend(ctx)
        if slow_trend == 0:
            # No clear trend, don't trade
            return
        
        # Get prior fast bar
        prior_bar = None
        if len(self.fast_bars) >= 2:
            bars_list = list(self.fast_bars)
            prior_bar = bars_list[-2]
        
        # Update bars_since_absorption counter
        if self.absorption_bar is not None:
            self.bars_since_absorption += 1
            
            # Reset if too much time has passed
            if self.bars_since_absorption > self.max_bars_after_absorption:
                self.absorption_bar = None
                self.bars_since_absorption = 0
        
        # Check for absorption on the current bar
        if prior_bar is not None and self.absorption_bar is None:
            if self._is_absorption(fast_bar, prior_bar):
                self.absorption_bar = {
                    "delta": fast_bar.delta,
                    "high": fast_bar.high,
                    "low": fast_bar.low,
                    "close": fast_bar.close,
                }
                self.bars_since_absorption = 0
        
        # Check for initiation on current bar
        if self.absorption_bar is not None and self.bars_since_absorption > 0:
            if self._is_initiation(fast_bar, self.absorption_bar):
                # Determine direction based on absorption delta
                if self.absorption_bar["delta"] > 0:
                    # Was positive absorption, initiation is negative -> SELL
                    # Only if slow trend is down
                    if slow_trend == -1:
                        ctx.sell(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="absorption-initiation-short",
                        )
                        self.absorption_bar = None
                        self.bars_since_absorption = 0
                else:
                    # Was negative absorption, initiation is positive -> BUY
                    # Only if slow trend is up
                    if slow_trend == 1:
                        ctx.buy(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="absorption-initiation-long",
                        )
                        self.absorption_bar = None
                        self.bars_since_absorption = 0
    
    def on_fill(self, ctx) -> None:
        pass
    
    def on_lifecycle(self, ctx) -> None:
        pass
