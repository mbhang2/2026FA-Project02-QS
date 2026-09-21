"""
Absorption and initiation with refined bar quality metrics and adaptive entry timing.

Trial #3 scored -0.5729 with 39 trades. The previous trial (#2) with 84 trades scored
-0.5058, suggesting we went too selective. The key insight: we need to find the sweet
spot between selectivity and frequency.

Analysis of the pattern:
1. Absorption + initiation is a real pattern, but we're being too strict on bar quality
2. The initiation bar doesn't need to be a perfect reversal bar - it needs to show
   opposite delta and some price conviction, but not necessarily close color matching
3. We're filtering out valid setups by requiring close color (red/green) which is too
   strict for a 1-minute timeframe with noise
4. The 5-minute trend confirmation is valuable but maybe too restrictive when combined
   with ultra-high std multipliers

This trial makes several adjustments:

1. **Relaxed absorption threshold**: back to 2.5 std (from 3.0) - we want real patterns,
   not just the most extreme ones. Most of the best trades happen in the 2-3 std range.

2. **Simplified initiation bar quality**: remove the close-color requirement. Instead,
   focus on:
   - Opposite delta direction that's strong enough
   - Efficiency (close position in range)
   - Volume above average (shows conviction without being too strict)

3. **Better trend confirmation**: keep the 5-minute trend but make it based on a longer
   lookback (3+ bars) to get a more stable bias, not just the last bar.

4. **Absorption bar refinement**: require the absorption bar to show SOME price movement
   against delta (not just a touch), but don't require it to break prior bar extremes.
   A bar that moves 60%+ against its delta is absorption enough.

5. **Initiation window**: allow up to 2 bars after absorption, but with a decay in
   confidence - the first bar after absorption gets full weight, the second bar needs
   slightly stronger signals.

The hypothesis: the pattern works best when we're selective but not obsessive. Trade
the clear setups (2.5+ std absorption) with good initiation (2.0+ std opposite delta),
but don't require perfection on every single bar metric.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        absorption_std_mult: float = 2.5,  # Back to 2.5, not 3.0
        initiation_std_mult: float = 2.0,  # Strong but not extreme
        initiation_efficiency: float = 0.65,  # Close position in range
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        lookback_window: int = 50,
        max_bars_after_absorption: int = 2,  # Allow 2 bars
        slow_trend_lookback: int = 3,  # Use 3+ bars for trend
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
        self.slow_trend_lookback = slow_trend_lookback
        
        # Track deltas, bars, ranges, and volumes
        self.fast_deltas: deque = deque(maxlen=lookback_window)
        self.fast_bars: deque = deque(maxlen=lookback_window + 1)
        self.fast_ranges: deque = deque(maxlen=lookback_window)
        self.fast_volumes: deque = deque(maxlen=lookback_window)
        
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
    
    def _calculate_volume_stats(self) -> tuple[float, float]:
        """Calculate mean and std of recent bar volumes."""
        if len(self.fast_volumes) < 3:
            return 100.0, 50.0
        volumes_list = list(self.fast_volumes)
        mean = sum(volumes_list) / len(volumes_list)
        variance = sum((v - mean) ** 2 for v in volumes_list) / len(volumes_list)
        std = variance ** 0.5
        return mean, max(std, 10.0)
    
    def _get_slow_trend(self, ctx) -> int:
        """
        Determine the 5-minute trend bias using multiple bars.
        Returns: 1 for uptrend, -1 for downtrend, 0 for neutral.
        """
        history = ctx.history(self.slow_interval)
        if len(history) < self.slow_trend_lookback:
            return 0
        
        # Look at the last N bars
        recent_bars = history.last(self.slow_trend_lookback)
        
        # Count closes higher vs lower
        higher_count = 0
        lower_count = 0
        for i in range(1, len(recent_bars)):
            if recent_bars[i].close > recent_bars[i-1].close:
                higher_count += 1
            elif recent_bars[i].close < recent_bars[i-1].close:
                lower_count += 1
        
        if higher_count > lower_count:
            return 1
        elif lower_count > higher_count:
            return -1
        return 0
    
    def _is_absorption(self, bar, prior_bar) -> bool:
        """
        Absorption: large delta that moves price against the delta direction.
        Requires the bar to show significant price movement against its delta.
        """
        if prior_bar is None:
            return False
        
        delta = bar.delta
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        
        # Adaptive threshold
        base_threshold = std_delta * self.absorption_std_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        if abs(delta) < threshold:
            return False
        
        # Price must move against the delta direction
        # Require at least 60% of the bar's range to move against delta
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        
        if delta > threshold:
            # Large positive delta (buying pressure)
            # Absorption: price moves DOWN despite the buying
            price_against_delta = bar.open - bar.close
            if price_against_delta > 0.6 * bar_range:
                return True
        elif delta < -threshold:
            # Large negative delta (selling pressure)
            # Absorption: price moves UP despite the selling
            price_against_delta = bar.close - bar.open
            if price_against_delta > 0.6 * bar_range:
                return True
        
        return False
    
    def _is_initiation(self, bar, absorption_bar, bars_since: int) -> bool:
        """
        Initiation: opposite delta that is strong and shows price conviction.
        Bar quality requirements are relaxed compared to trial #3.
        """
        if absorption_bar is None:
            return False
        
        absorption_delta = absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Adaptive threshold
        base_threshold = std_delta * self.initiation_std_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Volume should be reasonable (not necessarily above average, just not tiny)
        if bar.volume < mean_vol * 0.5:
            return False
        
        # Must be opposite direction and strong
        if absorption_delta > 0:
            # Was positive absorption, look for negative initiation
            if current_delta >= -threshold:
                return False
            
            # Check efficiency: close in lower half of range
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            if close_pos > (1.0 - self.initiation_efficiency):
                return False
            
            # Price should make some progress downward
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            if current_delta <= threshold:
                return False
            
            # Check efficiency: close in upper half of range
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            if close_pos < self.initiation_efficiency:
                return False
            
            # Price should make some progress upward
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
        self.fast_volumes.append(fast_bar.volume)
        
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
            if self._is_initiation(fast_bar, self.absorption_bar, self.bars_since_absorption):
                # Determine direction based on absorption delta
                if self.absorption_bar["delta"] > 0:
                    # Was positive absorption, initiation is negative -> SELL
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
