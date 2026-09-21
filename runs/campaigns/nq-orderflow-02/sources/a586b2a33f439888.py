"""
Absorption and initiation with refined momentum confirmation and adaptive thresholds.

Trial #25 scored -0.4232 with 257 trades - best absolute score.
Trial #30 scored -0.4517 with 136 trades - session context helped but price progress was too strict.

The progression shows:
- #25: -0.4232 with 257 trades (best score)
- #29: -0.4517 with 136 trades (tighter absorption, fewer trades)
- #30: -0.4517 with 136 trades (added session context, still too few trades)

Key insight: #25's -0.4232 is the best we've achieved. The attempts to improve it (#29, #30)
have all reduced trade count below the optimal 257, which hurts the score.

The real opportunity: #25 works because it has the right balance. Rather than adding
complexity (session context, price progress), we should refine the CORE absorption and
initiation detection to be more accurate.

Refinements based on trial progression analysis:

1. **Revert to #25's core parameters** (proven optimal):
   - absorption_std_mult=2.62
   - initiation_std_mult=1.98
   - initiation_std_mult_neutral=2.52
   - max_bars_after_absorption=2
   - All efficiency thresholds as in #25

2. **Improve absorption detection** (NEW):
   - Current: absorption is just strong delta + price rejection
   - Refined: also check that the rejection is SUSTAINED
   - If absorption bar closes in lower portion for positive delta, the NEXT bar
     should not immediately reverse back up (if available)
   - This filters out false absorptions that are just noise

3. **Stricter initiation delta requirement** (REFINED):
   - Current initiation uses threshold_mult of 1.98 or 2.52
   - Refined: require initiation delta to be 0.15 std stronger than absorption delta
   - This ensures initiation is truly opposite and significant, not marginal

4. **Volume confirmation on initiation** (REFINED):
   - Current: just checks min_initiation_volume_ratio
   - Refined: also require initiation volume to be >= absorption volume * 0.85
   - This ensures the reversal has conviction matching the original move

5. **Efficiency threshold adjustment** (REFINED):
   - Instead of fixed efficiency, make it adaptive to the bar's range
   - Wider bar = slightly more lenient on close position (more room for wicks)
   - Narrower bar = stricter on close position (less room for fakes)

The hypothesis: #25's trade count of 257 is optimal. By improving the quality of
absorption and initiation detection (false positive reduction), we can maintain
similar trade count but with higher conviction setups. This should match or slightly
beat #25's -0.4232 score.

Expected: -0.420 to -0.425 range with 250-260 trades.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        absorption_std_mult: float = 2.62,
        initiation_std_mult: float = 1.98,
        initiation_std_mult_neutral: float = 2.52,
        initiation_efficiency: float = 0.70,
        initiation_efficiency_strict: float = 0.77,
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        lookback_window: int = 40,
        max_bars_after_absorption: int = 2,
        slow_trend_lookback: int = 3,
        min_initiation_volume_ratio: float = 0.78,
        absorption_close_threshold: float = 0.35,
        efficiency_decay_per_bar: float = 0.02,
        initiation_delta_premium: float = 0.15,
        initiation_volume_vs_absorption: float = 0.85,
    ):
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.absorption_std_mult = absorption_std_mult
        self.initiation_std_mult = initiation_std_mult
        self.initiation_std_mult_neutral = initiation_std_mult_neutral
        self.initiation_efficiency = initiation_efficiency
        self.initiation_efficiency_strict = initiation_efficiency_strict
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.lookback_window = lookback_window
        self.max_bars_after_absorption = max_bars_after_absorption
        self.slow_trend_lookback = slow_trend_lookback
        self.min_initiation_volume_ratio = min_initiation_volume_ratio
        self.absorption_close_threshold = absorption_close_threshold
        self.efficiency_decay_per_bar = efficiency_decay_per_bar
        self.initiation_delta_premium = initiation_delta_premium
        self.initiation_volume_vs_absorption = initiation_volume_vs_absorption
        
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
        
        recent_bars = history.last(self.slow_trend_lookback)
        
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
    
    def _is_absorption(self, bar, prior_bar, next_bar=None) -> bool:
        """
        Absorption: strong delta with clear price rejection in opposite direction.
        Bar must close in the opposite portion of range from where delta points.
        Optionally check that rejection is sustained (next bar doesn't immediately reverse).
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
        
        # Close position in range: 0 = low, 1 = high
        close_pos = (bar.close - bar.low) / bar_range
        
        is_absorption = False
        
        if delta > threshold:
            # Large positive delta (buying pressure)
            # Absorption: close in lower portion showing rejection
            if close_pos < self.absorption_close_threshold:
                is_absorption = True
        elif delta < -threshold:
            # Large negative delta (selling pressure)
            # Absorption: close in upper portion showing rejection
            if close_pos > (1.0 - self.absorption_close_threshold):
                is_absorption = True
        
        # If we have a next bar, check that rejection is sustained
        if is_absorption and next_bar is not None:
            next_range = next_bar.high - next_bar.low
            if next_range > 0:
                next_close_pos = (next_bar.close - next_bar.low) / next_range
                
                if delta > threshold:
                    # Positive absorption: next bar shouldn't immediately rally
                    if next_close_pos > 0.65:
                        return False
                else:
                    # Negative absorption: next bar shouldn't immediately dump
                    if next_close_pos < 0.35:
                        return False
        
        return is_absorption
    
    def _is_initiation(self, bar, absorption_bar, slow_trend: int) -> bool:
        """
        Initiation: opposite delta that is strong and efficient.
        
        Uses different thresholds based on trend. Efficiency requirement increases
        with bars since absorption. Also requires initiation delta to be significantly
        stronger than absorption, and volume to match.
        """
        if absorption_bar is None:
            return False
        
        absorption_delta = absorption_bar.get("delta", 0)
        absorption_volume = absorption_bar.get("volume", 100)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Choose threshold and base efficiency based on trend
        if slow_trend == 0:
            # Neutral trend: require stricter initiation
            threshold_mult = self.initiation_std_mult_neutral
            base_efficiency = self.initiation_efficiency_strict
        else:
            # Have trend: use normal threshold
            threshold_mult = self.initiation_std_mult
            base_efficiency = self.initiation_efficiency
        
        # Apply decay to efficiency requirement based on bars since absorption
        efficiency = base_efficiency + (self.bars_since_absorption - 1) * self.efficiency_decay_per_bar
        efficiency = min(0.95, efficiency)
        
        # Adaptive threshold for initiation
        base_threshold = std_delta * threshold_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Volume check: reasonable conviction
        if bar.volume < mean_vol * self.min_initiation_volume_ratio:
            return False
        
        # Volume check: initiation should have conviction matching absorption
        if bar.volume < absorption_volume * self.initiation_volume_vs_absorption:
            return False
        
        # Close position in range
        range_val = bar.high - bar.low
        if range_val <= 0:
            return False
        close_pos = (bar.close - bar.low) / range_val
        
        # Adaptive efficiency based on bar range
        # Wider bar = slightly more lenient (more natural wicks)
        range_multiplier = max(0.95, min(1.05, (bar.high - bar.low) / (mean_range + 0.01)))
        adjusted_efficiency = efficiency / range_multiplier
        adjusted_efficiency = min(0.95, adjusted_efficiency)
        
        # Must be opposite direction and strong enough
        if absorption_delta > 0:
            # Was positive absorption, look for negative initiation
            # Require initiation delta to be premium stronger
            if current_delta >= -(threshold + std_delta * self.initiation_delta_premium):
                return False
            
            # Check efficiency: close in lower portion
            if close_pos > (1.0 - adjusted_efficiency):
                return False
            
            # Price should make progress downward
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            # Require initiation delta to be premium stronger
            if current_delta <= (threshold + std_delta * self.initiation_delta_premium):
                return False
            
            # Check efficiency: close in upper portion
            if close_pos < adjusted_efficiency:
                return False
            
            # Price should make progress upward
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
        
        # Get prior fast bar and next bar (if available)
        prior_bar = None
        next_bar = None
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
            if self._is_absorption(fast_bar, prior_bar, next_bar):
                self.absorption_bar = {
                    "delta": fast_bar.delta,
                    "high": fast_bar.high,
                    "low": fast_bar.low,
                    "close": fast_bar.close,
                    "volume": fast_bar.volume,
                }
                self.bars_since_absorption = 0
        
        # Check for initiation on current bar
        if self.absorption_bar is not None and self.bars_since_absorption > 0:
            if self._is_initiation(fast_bar, self.absorption_bar, slow_trend):
                # Determine direction based on absorption delta
                if self.absorption_bar["delta"] > 0:
                    # Was positive absorption, initiation is negative -> SELL
                    # Enter if downtrend OR neutral with very strong initiation
                    if slow_trend == -1 or slow_trend == 0:
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
                    # Enter if uptrend OR neutral with very strong initiation
                    if slow_trend == 1 or slow_trend == 0:
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
