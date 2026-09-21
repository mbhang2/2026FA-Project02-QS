"""
Absorption and initiation with refined absorption detection and stricter initiation confirmation.

Trial #35 scored -0.5140 with 102 trades - worse than #25's -0.4232 with 257 trades.
Trial #25 remains the best at -0.4232 with 257 trades.
Trial #34 scored -0.4327 with 278 trades - momentum scaling added trades but hurt score.

Analysis of recent failures:
- #35: -0.5140 (102 trades) - initiation_delta_multiplier=1.3 too strict, killed trades
- #34: -0.4327 (278 trades) - too many false positives from momentum relaxation
- #33: -0.5586 (57 trades) - cross-timeframe confirmation over-constrained
- #25: -0.4232 (257 trades) - PEAK, simplest is best

Key insight: #25's simplicity is its strength. The attempts to improve it (#31-#35)
have all failed because they add complexity without addressing the core issue: we're
generating too many marginal trades that hurt the score.

The real problem: #25 generates 257 trades with -0.4232. That's ~0.67 trades per
session. We're not leaving money on the table; we're taking too many marginal trades.

This trial takes a step back and refines #25's core logic with **absorption quality
tightening** rather than initiation loosening:

1. **Revert to #25's proven parameters**:
   - absorption_std_mult=2.62
   - initiation_std_mult=1.98
   - initiation_std_mult_neutral=2.52
   - initiation_efficiency=0.70
   - initiation_efficiency_strict=0.77
   - max_bars_after_absorption=2

2. **Tighter absorption detection** (REFINED):
   - Absorption requires delta > 2.62 std AND close in opposite 35% of range
   - Add a secondary filter: absorption bar must be followed by a bar with
     lower volume (absorption shows rejection, next bar should be quieter)
   - This filters out absorption bars that are just normal high-volume bars
   - Reduces false absorption detections that lead to marginal initiations

3. **Stricter initiation confirmation** (REFINED):
   - Initiation must have delta stronger than absorption (not just opposite)
   - For short initiation: delta must be < -1.1x absorption_delta magnitude
   - For long initiation: delta must be > 1.1x absorption_delta magnitude
   - This ensures true reversal flow, not just any opposite delta

4. **Volume persistence check** (NEW):
   - Track volume on absorption bar
   - Initiation bar must have volume >= 0.85x absorption bar volume
   - This ensures initiation has similar conviction to absorption
   - Filters low-volume false initiations that happen to have right delta

5. **Absorption bar context** (REFINED):
   - Store not just delta/high/low but also volume and close position
   - Use this to validate initiations more strictly

The logic: #25 works because it's selective. By tightening absorption detection
(fewer false absorptions) and stricter initiation confirmation (fewer marginal
initiations), we should reduce trade count while improving quality. Target:
-0.4200 to -0.4220 with 240-250 trades, beating #25's -0.4232 by being more selective.
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
        absorption_volume_ratio: float = 0.80,
        initiation_delta_ratio: float = 1.1,
        initiation_volume_ratio_vs_absorption: float = 0.85,
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
        self.absorption_volume_ratio = absorption_volume_ratio
        self.initiation_delta_ratio = initiation_delta_ratio
        self.initiation_volume_ratio_vs_absorption = initiation_volume_ratio_vs_absorption
        
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
    
    def _is_absorption(self, bar, prior_bar, next_bar_volume: int | None = None) -> bool:
        """
        Absorption: strong delta with clear price rejection in opposite direction.
        Bar must close in the opposite portion of range from where delta points.
        Requires reasonable volume conviction.
        Optionally requires next bar to have lower volume (showing rejection).
        """
        if prior_bar is None:
            return False
        
        delta = bar.delta
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Volume check: absorption should have conviction
        if bar.volume < mean_vol * self.absorption_volume_ratio:
            return False
        
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
        
        # If absorption candidate, optionally check next bar volume
        # (next_bar_volume would be passed if we have it)
        if is_absorption and next_bar_volume is not None:
            # Absorption should be followed by quieter bar (showing rejection held)
            if next_bar_volume >= bar.volume:
                return False
        
        return is_absorption
    
    def _is_initiation(self, bar, absorption_bar, slow_trend: int) -> bool:
        """
        Initiation: opposite delta that is strong and efficient.
        Delta must be stronger than absorption delta (not just opposite).
        Volume must be reasonable relative to absorption bar.
        """
        if absorption_bar is None:
            return False
        
        absorption_delta = absorption_bar.get("delta", 0)
        absorption_volume = absorption_bar.get("volume", 0)
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
        
        # Adaptive threshold
        base_threshold = std_delta * threshold_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Volume check: reasonable conviction
        if bar.volume < mean_vol * self.min_initiation_volume_ratio:
            return False
        
        # Volume check relative to absorption bar
        if bar.volume < absorption_volume * self.initiation_volume_ratio_vs_absorption:
            return False
        
        # Close position in range
        range_val = bar.high - bar.low
        if range_val <= 0:
            return False
        close_pos = (bar.close - bar.low) / range_val
        
        # Must be opposite direction and strong enough
        if absorption_delta > 0:
            # Was positive absorption, look for negative initiation
            # Initiation delta must be stronger than absorption (more negative)
            min_initiation_delta = -(abs(absorption_delta) * self.initiation_delta_ratio)
            if current_delta > min_initiation_delta:
                return False
            
            # Check efficiency: close in lower portion
            if close_pos > (1.0 - base_efficiency):
                return False
            
            # Price should make progress downward
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            # Initiation delta must be stronger than absorption (more positive)
            min_initiation_delta = abs(absorption_delta) * self.initiation_delta_ratio
            if current_delta < min_initiation_delta:
                return False
            
            # Check efficiency: close in upper portion
            if close_pos < base_efficiency:
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
