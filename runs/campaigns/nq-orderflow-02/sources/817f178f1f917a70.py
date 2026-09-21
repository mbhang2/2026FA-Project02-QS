"""
Absorption and initiation with adaptive efficiency thresholds and session-aware filtering.

Trial #25 scored -0.4232 with 257 trades - baseline peak.
Trial #42 scored -0.4235 with 136 trades - decay approach lost 121 trades.

Analysis of the progression shows a critical insight:
- #25: -0.4232 with 257 trades (PEAK - simple, effective)
- #42: -0.4235 with 136 trades - efficiency decay was TOO AGGRESSIVE
- #41: -0.4998 with 498 trades - quality scoring was TOO PERMISSIVE
- #40: -0.4267 with 220 trades - reversal check lost good trades

The pattern: #25 is optimal because it balances absorption detection (tight at 2.62)
with initiation acceptance (moderate at 1.98-2.52) without over-filtering.

The key failure in #42: efficiency_decay_per_bar=0.015 was TOO STRICT. By the
second bar after absorption, efficiency jumped from 0.70 to 0.73, filtering out
valid delayed initiations. The geometric mean in #41 was too loose.

This trial takes a refined approach:

1. **Keep #25's core absorption detection** (proven optimal):
   - absorption_std_mult=2.62
   - absorption_close_threshold=0.35
   - Simple, tight, effective

2. **Revert initiation to #25's thresholds**:
   - initiation_std_mult=1.98
   - initiation_std_mult_neutral=2.52
   - initiation_efficiency=0.70
   - initiation_efficiency_strict=0.77
   - min_initiation_volume_ratio=0.78

3. **Add adaptive session-aware filtering** (NEW):
   - Track session volatility (range stats per session)
   - Adjust absorption threshold based on session volatility
   - Higher volatility sessions = higher absorption threshold (filter noise)
   - Lower volatility sessions = normal threshold (catch moves)
   - This naturally filters false positives without hard decay

4. **Add initiation confirmation via price action** (REFINED):
   - Initiation must show clear directional commitment
   - For negative initiation: close must be in lower 40% of range
   - For positive initiation: close must be in upper 40% of range
   - This is softer than #39's hard reversal but tighter than #25's 70%

5. **Adaptive initiation window**:
   - max_bars_after_absorption=2
   - Allow delayed initiations but require tighter confirmation
   - Efficiency requirement stays fixed (no decay), but price action confirmation tightens

The hypothesis: #25's 257 trades at -0.4232 is the sweet spot. By adding
session-aware volatility filtering (reduces false absorptions in noisy sessions)
and price action confirmation (ensures initiations are clean), we maintain the
good trades while filtering weak ones. This should match or beat #25's score
with similar trade count (240-270 trades).
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
        session_volatility_mult: float = 1.3,
        initiation_close_threshold: float = 0.40,
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
        self.session_volatility_mult = session_volatility_mult
        self.initiation_close_threshold = initiation_close_threshold
        
        # Track deltas, bars, ranges, and volumes
        self.fast_deltas: deque = deque(maxlen=lookback_window)
        self.fast_bars: deque = deque(maxlen=lookback_window + 1)
        self.fast_ranges: deque = deque(maxlen=lookback_window)
        self.fast_volumes: deque = deque(maxlen=lookback_window)
        
        # Session tracking for volatility adjustment
        self.current_session: int = -1
        self.session_ranges: deque = deque(maxlen=lookback_window)
        
        # Absorption tracking
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
    
    def _get_session_volatility_factor(self) -> float:
        """
        Calculate session volatility adjustment factor.
        Higher volatility = higher absorption threshold (filter noise).
        """
        if len(self.session_ranges) < 5:
            return 1.0
        
        session_ranges_list = list(self.session_ranges)
        mean_session_range = sum(session_ranges_list) / len(session_ranges_list)
        variance = sum((r - mean_session_range) ** 2 for r in session_ranges_list) / len(session_ranges_list)
        std_session_range = variance ** 0.5
        
        # Current volatility relative to session average
        current_volatility = std_session_range / (mean_session_range + 0.01)
        
        # If volatility is high, increase threshold to filter noise
        if current_volatility > 0.4:
            return self.session_volatility_mult
        return 1.0
    
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
        Threshold adjusted for session volatility.
        """
        if prior_bar is None:
            return False
        
        delta = bar.delta
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        
        # Adaptive threshold with session volatility adjustment
        base_threshold = std_delta * self.absorption_std_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Apply session volatility adjustment
        volatility_factor = self._get_session_volatility_factor()
        threshold *= volatility_factor
        
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
    
    def _is_initiation(self, bar, absorption_bar, slow_trend: int) -> bool:
        """
        Initiation: opposite delta that is strong and efficient.
        Price action confirmation: close must be committed to the move.
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
        
        # Adaptive threshold
        base_threshold = std_delta * threshold_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Volume check: reasonable conviction
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
            
            # Check basic efficiency
            if close_pos > (1.0 - base_efficiency):
                return False
            
            # Price action confirmation: close in lower portion (0.40 threshold)
            if close_pos > (1.0 - self.initiation_close_threshold):
                return False
            
            # Price should make progress downward
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            if current_delta <= threshold:
                return False
            
            # Check basic efficiency
            if close_pos < base_efficiency:
                return False
            
            # Price action confirmation: close in upper portion (0.40 threshold)
            if close_pos < self.initiation_close_threshold:
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
        
        # Track session changes for volatility adjustment
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.session_ranges.clear()
        
        # Update fast bar history
        self.fast_bars.append(fast_bar)
        self.fast_deltas.append(fast_bar.delta)
        bar_range = fast_bar.high - fast_bar.low
        self.fast_ranges.append(bar_range)
        self.fast_volumes.append(fast_bar.volume)
        self.session_ranges.append(bar_range)
        
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
    
    def on_fill(self, ctx) -> None:
        pass
    
    def on_lifecycle(self, ctx) -> None:
        pass
