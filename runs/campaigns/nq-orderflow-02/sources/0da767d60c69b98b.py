"""
Absorption and initiation with adaptive multi-bar initiation window and efficiency scaling.

Trial #25 scored -0.4232 with 257 trades - best absolute score.
Trial #26 scored -0.4452 with 155 trades - tighter but lower score.

Key observation: #25's higher trade count (257 vs 155) with better score (-0.4232 vs -0.4452)
suggests we should be MORE selective about which initiations we take, not less. The issue
with #26 is that requiring a break of absorption range was too strict, cutting off valid
reversals that don't quite break the prior extreme.

The real problem: we're treating all initiations equally. Some initiations are stronger
than others. An initiation bar that:
- Makes a new session extreme in its direction
- Has very high volume
- Shows extreme delta efficiency

...should be taken even on neutral trend. A weak initiation should require trend confirmation.

This trial implements **efficiency-based entry filtering**:

1. **Keep #25's core parameters** (proven best):
   - absorption_std_mult=2.62
   - initiation_std_mult=1.98
   - initiation_std_mult_neutral=2.52
   - initiation_efficiency=0.70
   - initiation_efficiency_strict=0.77
   - max_bars_after_absorption=2
   - All other thresholds as in #25

2. **Add efficiency tiers for entry logic**:
   - Calculate initiation "quality score" based on:
     * How extreme the delta is relative to threshold (efficiency_ratio)
     * How close to session high/low the bar closes
     * Volume relative to mean
   - High quality (efficiency_ratio > 1.5): enter on neutral trend
   - Medium quality (1.2-1.5): require trend agreement
   - Lower quality (< 1.2): skip on neutral, require strong trend

3. **Track session extremes**:
   - Keep session high/low to identify if initiation makes new extremes
   - Bonus points for breaking session extremes

4. **Refined volume weighting**:
   - Don't just check minimum volume, weight the initiation quality by volume
   - High volume initiations get lower efficiency requirements

The logic: #25 found the right trade count and score range. Instead of restricting
WHERE we enter (range breaks), restrict WHO we enter (quality tiers). A super-clean
initiation gets in regardless of trend; a marginal one needs trend help. This should
hit -0.4250 to -0.4300 with 240-260 trades.
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
        efficiency_high_threshold: float = 1.5,
        efficiency_medium_threshold: float = 1.2,
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
        self.efficiency_high_threshold = efficiency_high_threshold
        self.efficiency_medium_threshold = efficiency_medium_threshold
        
        # Track deltas, bars, ranges, and volumes
        self.fast_deltas: deque = deque(maxlen=lookback_window)
        self.fast_bars: deque = deque(maxlen=lookback_window + 1)
        self.fast_ranges: deque = deque(maxlen=lookback_window)
        self.fast_volumes: deque = deque(maxlen=lookback_window)
        
        self.absorption_bar: dict | None = None
        self.bars_since_absorption: int = 0
        
        # Session tracking
        self.session_high: float = 0.0
        self.session_low: float = float('inf')
        self.current_session: int = -1
        
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
        Bar must close in the opposite portion of range from where delta points.
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
            # Large positive delta (buying pressure)
            # Absorption: close in lower portion showing rejection
            if close_pos < self.absorption_close_threshold:
                return True
        elif delta < -threshold:
            # Large negative delta (selling pressure)
            # Absorption: close in upper portion showing rejection
            if close_pos > (1.0 - self.absorption_close_threshold):
                return True
        
        return False
    
    def _calculate_initiation_quality(self, bar, absorption_bar, slow_trend: int) -> tuple[bool, float]:
        """
        Calculate initiation quality score.
        Returns: (is_initiation, quality_ratio)
        
        quality_ratio > 1.5: high quality (can enter on neutral)
        quality_ratio 1.2-1.5: medium quality (needs trend)
        quality_ratio < 1.2: low quality (needs strong trend)
        """
        if absorption_bar is None:
            return False, 0.0
        
        absorption_delta = absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Choose threshold based on trend
        if slow_trend == 0:
            threshold_mult = self.initiation_std_mult_neutral
            base_efficiency = self.initiation_efficiency_strict
        else:
            threshold_mult = self.initiation_std_mult
            base_efficiency = self.initiation_efficiency
        
        # Adaptive threshold
        base_threshold = std_delta * threshold_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Check opposite direction
        if absorption_delta > 0:
            if current_delta >= -threshold:
                return False, 0.0
        else:
            if current_delta <= threshold:
                return False, 0.0
        
        # Check efficiency
        range_val = bar.high - bar.low
        if range_val <= 0:
            return False, 0.0
        close_pos = (bar.close - bar.low) / range_val
        
        if absorption_delta > 0:
            # Looking for short (negative delta), close in lower portion
            if close_pos > (1.0 - base_efficiency):
                return False, 0.0
        else:
            # Looking for long (positive delta), close in upper portion
            if close_pos < base_efficiency:
                return False, 0.0
        
        # Calculate quality: how much stronger than threshold
        abs_current = abs(current_delta)
        quality_ratio = abs_current / abs(threshold) if threshold != 0 else 1.0
        
        # Volume bonus
        if bar.volume > mean_vol * 1.2:
            quality_ratio *= 1.1
        
        # Session extreme bonus
        if absorption_delta > 0 and bar.low < self.session_low:
            quality_ratio *= 1.15
        elif absorption_delta < 0 and bar.high > self.session_high:
            quality_ratio *= 1.15
        
        return True, quality_ratio
    
    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Get fast bar if it closed
        if self.fast_interval not in ctx.bars:
            return
        
        fast_bar = ctx.bars[self.fast_interval]
        
        # Update session extremes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.session_high = fast_bar.high
            self.session_low = fast_bar.low
        else:
            self.session_high = max(self.session_high, fast_bar.high)
            self.session_low = min(self.session_low, fast_bar.low)
        
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
                }
                self.bars_since_absorption = 0
        
        # Check for initiation on current bar
        if self.absorption_bar is not None and self.bars_since_absorption > 0:
            is_init, quality = self._calculate_initiation_quality(fast_bar, self.absorption_bar, slow_trend)
            
            if is_init:
                # Determine direction and entry rules based on quality
                if self.absorption_bar["delta"] > 0:
                    # Was positive absorption, initiation is negative -> SELL
                    can_enter = False
                    if quality > self.efficiency_high_threshold:
                        # High quality: enter regardless of trend
                        can_enter = True
                    elif quality > self.efficiency_medium_threshold:
                        # Medium quality: need downtrend or neutral
                        can_enter = (slow_trend == -1 or slow_trend == 0)
                    else:
                        # Low quality: need strong downtrend
                        can_enter = (slow_trend == -1)
                    
                    if can_enter:
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
                    can_enter = False
                    if quality > self.efficiency_high_threshold:
                        # High quality: enter regardless of trend
                        can_enter = True
                    elif quality > self.efficiency_medium_threshold:
                        # Medium quality: need uptrend or neutral
                        can_enter = (slow_trend == 1 or slow_trend == 0)
                    else:
                        # Low quality: need strong uptrend
                        can_enter = (slow_trend == 1)
                    
                    if can_enter:
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
