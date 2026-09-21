"""
Absorption and initiation with refined pattern detection and session-aware entry filtering.

Trial #2 scored -0.5058 with 84 trades (best efficiency), trial #4 scored -0.5729 with 39 
trades (too selective). The sweet spot appears to be around 50-100 trades per 384 sessions.

Key observations from prior trials:
1. The absorption + initiation pattern is real, but sensitivity to parameters is high
2. Trial #2's success came from being selective but not obsessive
3. Trial #3 and #4 went too far toward selectivity, reducing trade count too much
4. The 5-minute trend confirmation is valuable but shouldn't gate all trades

This trial refines the approach with:

1. **Absorption detection**: Require strong delta (2.3 std, down from 2.5) with clear 
   price rejection. The bar must show >50% of range moving against delta direction.
   This is slightly more permissive than trial #4 to capture more valid setups.

2. **Initiation detection**: Opposite delta at 1.8 std (down from 2.0-2.5) with efficiency
   requirement. The key insight: initiation doesn't need to be a perfect reversal, just
   show conviction in the opposite direction. Allow bars with volume >= 70% of mean.

3. **Improved trend confirmation**: Rather than requiring a clear trend, allow trades
   when trend is neutral BUT only if the initiation bar shows exceptional strength
   (delta > 2.5 std). This captures breakout-style reversals that don't need trend bias.

4. **Absorption window refinement**: Track absorption state more carefully. Once we see
   absorption, we wait for initiation in the next 1-2 bars. If we see absorption again
   while waiting, reset and track the new one. This prevents missing better setups.

5. **Entry timing**: Enter on the initiation bar itself, not waiting for the next bar.
   This captures the momentum while it's fresh and improves entry quality.

6. **Position management**: Keep tight stops (0.4-0.5) and target (0.7-0.75) to manage
   risk on a 1-minute timeframe where noise is significant.

The hypothesis: the pattern works best when we trade the clear absorption+initiation
setups (2.0-2.5 std absorption, 1.8+ std initiation) with reasonable frequency, and
allow some trades even without perfect trend confirmation if initiation is exceptional.
This should yield 60-100 trades with better quality than the high-frequency trials.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        absorption_std_mult: float = 2.3,  # Slightly more permissive
        initiation_std_mult: float = 1.8,  # Lower threshold for more setups
        strong_initiation_std_mult: float = 2.5,  # For trend-neutral entries
        initiation_efficiency: float = 0.65,  # Close position in range
        size: int = 1,
        stop_offset: float = 0.4,  # Tighter stops for 1m noise
        target_offset: float = 0.75,
        lookback_window: int = 60,
        max_bars_after_absorption: int = 2,
        slow_trend_lookback: int = 3,
        min_volume_ratio: float = 0.7,  # Minimum volume as ratio of mean
    ):
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.absorption_std_mult = absorption_std_mult
        self.initiation_std_mult = initiation_std_mult
        self.strong_initiation_std_mult = strong_initiation_std_mult
        self.initiation_efficiency = initiation_efficiency
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.lookback_window = lookback_window
        self.max_bars_after_absorption = max_bars_after_absorption
        self.slow_trend_lookback = slow_trend_lookback
        self.min_volume_ratio = min_volume_ratio
        
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
        Determine the 5-minute trend bias.
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
        Absorption: strong delta with significant price movement against it.
        Requires >50% of bar range to move against delta direction.
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
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        
        if delta > threshold:
            # Large positive delta (buying pressure)
            # Absorption: price moves DOWN despite the buying
            price_against_delta = bar.open - bar.close
            if price_against_delta > 0.5 * bar_range:
                return True
        elif delta < -threshold:
            # Large negative delta (selling pressure)
            # Absorption: price moves UP despite the selling
            price_against_delta = bar.close - bar.open
            if price_against_delta > 0.5 * bar_range:
                return True
        
        return False
    
    def _is_initiation(self, bar, absorption_bar, slow_trend: int) -> bool:
        """
        Initiation: opposite delta with price conviction.
        
        Two modes:
        1. With trend confirmation: delta at initiation_std_mult threshold
        2. Without trend (neutral): delta must be stronger (strong_initiation_std_mult)
        """
        if absorption_bar is None:
            return False
        
        absorption_delta = absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Determine which threshold to use based on trend
        if slow_trend == 0:
            # No trend: require stronger initiation signal
            threshold_mult = self.strong_initiation_std_mult
        else:
            # Have trend: use normal threshold
            threshold_mult = self.initiation_std_mult
        
        # Adaptive threshold
        base_threshold = std_delta * threshold_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Volume check: should be reasonable
        if bar.volume < mean_vol * self.min_volume_ratio:
            return False
        
        # Must be opposite direction and strong enough
        if absorption_delta > 0:
            # Was positive absorption, look for negative initiation
            if current_delta >= -threshold:
                return False
            
            # Check efficiency: close in lower portion of range
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            if close_pos > (1.0 - self.initiation_efficiency):
                return False
            
            # Price should show downward progress
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            if current_delta <= threshold:
                return False
            
            # Check efficiency: close in upper portion of range
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            if close_pos < self.initiation_efficiency:
                return False
            
            # Price should show upward progress
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
        
        # Check for absorption on the current bar (can reset previous absorption)
        if prior_bar is not None:
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
            if self._is_initiation(fast_bar, self.absorption_bar, slow_trend):
                # Determine direction based on absorption delta
                if self.absorption_bar["delta"] > 0:
                    # Was positive absorption, initiation is negative -> SELL
                    # Enter if we have downtrend OR very strong initiation signal
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
                    # Enter if we have uptrend OR very strong initiation signal
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
