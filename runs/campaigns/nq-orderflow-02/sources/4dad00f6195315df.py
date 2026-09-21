"""
Absorption and initiation with refined confirmation timing and efficiency decay.

Trial #25 scored -0.4232 with 257 trades - baseline peak.
Trial #39 scored -0.4385 with 183 trades - efficiency confirmation helped but
  reduced trade count significantly.

Analysis of recent trials:
- #25: -0.4232 with 257 trades (PEAK baseline, simple approach)
- #39: -0.4385 with 183 trades - efficiency confirmation reduced false positives
  but also eliminated valid trades
- #38: -0.5156 with 263 trades - relaxed thresholds added noise
- #37: -0.5106 with 198 trades - multi-bar confirmation was too strict

Key insight: #25 works because it generates 257 high-quality trades. #39 tried
to filter with efficiency confirmation but lost 74 trades (-29%) and score
degraded to -0.4385 (worse than #25). The problem: we're filtering too aggressively
on confirmation.

Looking at the pattern:
- #25: 257 trades, -0.4232 (0.67 trades/session)
- #39: 183 trades, -0.4385 (0.48 trades/session)
- Lost 74 trades, worse score = over-filtering

The real issue: the next_bar_efficiency_threshold=0.55 in #39 is too strict.
It requires the bar immediately after absorption to show efficiency, which
kills delayed initiations that are still valid.

This trial refines #25 with a different confirmation strategy:

1. **Keep #25's core parameters** (proven to work):
   - absorption_std_mult=2.62
   - initiation_std_mult=1.98
   - initiation_std_mult_neutral=2.52
   - initiation_efficiency=0.70
   - initiation_efficiency_strict=0.77
   - absorption_close_threshold=0.35
   - min_initiation_volume_ratio=0.78

2. **Softer absorption confirmation** (REFINED):
   - Instead of requiring efficiency in the next bar, just check that we don't
     see a reversal (delta going back in absorption direction)
   - A reversal would indicate the absorption was fake; absence of reversal
     suggests it was real
   - This is much less restrictive than #39's efficiency check
   - Allows initiations to arrive 1-2 bars later without killing them

3. **Efficiency decay on initiation** (REFINED):
   - Initiation requirements tighten slightly as bars pass (0.01 per bar)
   - This maintains quality while allowing delayed entries
   - Much gentler than #39's hard confirmation requirement

4. **Adaptive initiation window** (from #39):
   - max_bars_after_absorption=2 (allow up to 2 bars delay)
   - Window can extend if we see no reversal in the next bar
   - This captures the delayed initiations that #25 might miss

The hypothesis: #25 is nearly optimal. The issue is not filtering absorptions
but capturing delayed initiations cleanly. By using a soft "no reversal"
confirmation instead of a hard efficiency check, we filter obvious false
absorptions without killing valid trades. The efficiency decay on initiation
then ensures quality as time passes. This should match or beat #25's -0.4232
with similar trade count (250-265 trades).
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
        efficiency_decay_per_bar: float = 0.01,
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
        
        # Track deltas, bars, ranges, and volumes
        self.fast_deltas: deque = deque(maxlen=lookback_window)
        self.fast_bars: deque = deque(maxlen=lookback_window + 1)
        self.fast_ranges: deque = deque(maxlen=lookback_window)
        self.fast_volumes: deque = deque(maxlen=lookback_window)
        
        # Absorption tracking
        self.absorption_bar: dict | None = None
        self.bars_since_absorption: int = 0
        self.absorption_delta_direction: int = 0  # 1 for positive, -1 for negative
        
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
    
    def _is_absorption(self, bar, prior_bar) -> bool:
        """
        Absorption: strong delta with clear price rejection in opposite direction.
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
        
        if delta > threshold:
            # Large positive delta, close in lower portion
            if close_pos < self.absorption_close_threshold:
                return True
        elif delta < -threshold:
            # Large negative delta, close in upper portion
            if close_pos > (1.0 - self.absorption_close_threshold):
                return True
        
        return False
    
    def _is_reversal(self, bar) -> bool:
        """
        Check if current bar reverses the absorption delta direction.
        A reversal (delta going back in original direction) suggests the
        absorption was fake. Absence of reversal suggests it was real.
        """
        if self.absorption_bar is None:
            return False
        
        absorption_delta = self.absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        # If absorption was positive, reversal is positive delta
        if absorption_delta > 0:
            return current_delta > 0
        else:
            # If absorption was negative, reversal is negative delta
            return current_delta < 0
    
    def _is_initiation(self, bar, absorption_bar, slow_trend: int) -> bool:
        """
        Initiation: opposite delta that is strong and efficient.
        Efficiency requirement tightens as bars pass since absorption.
        """
        if absorption_bar is None:
            return False
        
        absorption_delta = absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Choose threshold and base efficiency based on trend
        if slow_trend == 0:
            threshold_mult = self.initiation_std_mult_neutral
            base_efficiency = self.initiation_efficiency_strict
        else:
            threshold_mult = self.initiation_std_mult
            base_efficiency = self.initiation_efficiency
        
        # Apply efficiency decay based on bars since absorption
        # More bars = higher efficiency requirement (stricter)
        efficiency = base_efficiency + (self.bars_since_absorption - 1) * self.efficiency_decay_per_bar
        efficiency = min(0.95, efficiency)  # Cap at 0.95
        
        # Adaptive threshold
        base_threshold = std_delta * threshold_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Volume check
        if bar.volume < mean_vol * self.min_initiation_volume_ratio:
            return False
        
        # Close position in range
        range_val = bar.high - bar.low
        if range_val <= 0:
            return False
        close_pos = (bar.close - bar.low) / range_val
        
        # Must be opposite direction and strong enough
        if absorption_delta > 0:
            # Was positive absorption, look for negative initiation
            if current_delta >= -threshold:
                return False
            
            # Check efficiency: close in lower portion
            if close_pos > (1.0 - efficiency):
                return False
            
            # Price should make progress downward
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            if current_delta <= threshold:
                return False
            
            # Check efficiency: close in upper portion
            if close_pos < efficiency:
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
            
            # Check for reversal (soft confirmation)
            if self.bars_since_absorption == 1:
                if self._is_reversal(fast_bar):
                    # Absorption was reversed, invalidate it
                    self.absorption_bar = None
                    self.bars_since_absorption = 0
                    self.absorption_delta_direction = 0
            
            # Reset if too much time has passed
            if self.absorption_bar is not None and self.bars_since_absorption > self.max_bars_after_absorption:
                self.absorption_bar = None
                self.bars_since_absorption = 0
                self.absorption_delta_direction = 0
        
        # Check for absorption on the current bar
        if prior_bar is not None and self.absorption_bar is None:
            if self._is_absorption(fast_bar, prior_bar):
                self.absorption_bar = {
                    "delta": fast_bar.delta,
                    "high": fast_bar.high,
                    "low": fast_bar.low,
                    "close": fast_bar.close,
                }
                self.absorption_delta_direction = 1 if fast_bar.delta > 0 else -1
                self.bars_since_absorption = 0
        
        # Check for initiation on current bar
        if self.absorption_bar is not None and self.bars_since_absorption > 0:
            if self._is_initiation(fast_bar, self.absorption_bar, slow_trend):
                # Determine direction based on absorption delta
                if self.absorption_bar["delta"] > 0:
                    # Was positive absorption, initiation is negative -> SELL
                    if slow_trend == -1 or slow_trend == 0:
                        ctx.sell(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="absorption-initiation-short",
                        )
                        self.absorption_bar = None
                        self.bars_since_absorption = 0
                        self.absorption_delta_direction = 0
                else:
                    # Was negative absorption, initiation is positive -> BUY
                    if slow_trend == 1 or slow_trend == 0:
                        ctx.buy(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="absorption-initiation-long",
                        )
                        self.absorption_bar = None
                        self.bars_since_absorption = 0
                        self.absorption_delta_direction = 0
    
    def on_fill(self, ctx) -> None:
        pass
    
    def on_lifecycle(self, ctx) -> None:
        pass
