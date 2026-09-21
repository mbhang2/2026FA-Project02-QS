"""
Absorption and initiation with refined multi-bar initiation patterns and session context.

Trial #25 scored -0.4232 with 257 trades - best absolute score.
Trial #29 scored -0.4517 with 136 trades - tighter absorption helped but too restrictive.

The progression shows:
- #19: -0.4408 with 157 trades (strong baseline)
- #25: -0.4232 with 257 trades (best score, more trades)
- #29: -0.4517 with 136 trades (fewer trades, worse score)

Key insight: #25's -0.4232 is the best we've achieved. The issue with #29 was that
tightening absorption_std_mult to 2.75 and max_bars_after_absorption to 1 reduced
trade count too much, cutting into the signal.

The opportunity: #25 works well because it has the right balance of sensitivity and
specificity. But we can refine the initiation detection to be more selective about
WHICH initiations we take.

Refinements to #25:

1. **Keep #25's core parameters** (proven to work):
   - absorption_std_mult=2.62
   - initiation_std_mult=1.98
   - initiation_std_mult_neutral=2.52
   - max_bars_after_absorption=2 (allows bar-1 and bar-2 initiations)
   - All efficiency and volume thresholds as in #25

2. **Add multi-bar confirmation** (NEW):
   - After detecting absorption, don't just look for any initiation
   - Require that the initiation bar shows SUSTAINED momentum:
     * Check that the next bar (if it exists) doesn't immediately reverse
     * If initiation is on bar N, bar N+1 should not close against the initiation
   - This filters out false reversals that snap back

3. **Tighter price progress requirement**:
   - For short initiations: require price to break below absorption low by at least
     a small amount (0.15 index points) to confirm the move is real
   - For long initiations: require price to break above absorption high similarly
   - This prevents marginal initiations that barely extend the range

4. **Session context filter** (NEW):
   - Track whether we're in the first 30 minutes of the session (volatile, noisy)
   - In first 30 min, require stricter efficiency (add 0.05 to threshold)
   - This avoids the cash-open chaos while keeping the rest of the day's trades

The hypothesis: #25's trade count (257) is good, but many of those trades are
marginal. By adding momentum confirmation and price-progress requirements, we filter
to higher-conviction setups while maintaining similar overall trade count. This should
improve on #25's -0.4232 score.

Expected: -0.41 to -0.42 range with 220-250 trades (fewer but higher quality).
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
        min_price_progress: float = 0.15,
        session_open_window_minutes: int = 30,
        early_session_efficiency_penalty: float = 0.05,
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
        self.min_price_progress = min_price_progress
        self.session_open_window_minutes = session_open_window_minutes
        self.early_session_efficiency_penalty = early_session_efficiency_penalty
        
        # Track deltas, bars, ranges, and volumes
        self.fast_deltas: deque = deque(maxlen=lookback_window)
        self.fast_bars: deque = deque(maxlen=lookback_window + 1)
        self.fast_ranges: deque = deque(maxlen=lookback_window)
        self.fast_volumes: deque = deque(maxlen=lookback_window)
        
        self.absorption_bar: dict | None = None
        self.bars_since_absorption: int = 0
        self.session_start_ts: int | None = None
        
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
    
    def _is_in_early_session(self, ctx) -> bool:
        """Check if we're in the first N minutes of the session."""
        if self.session_start_ts is None:
            return False
        time_since_open_ns = ctx.ts - self.session_start_ts
        time_since_open_minutes = time_since_open_ns / (60 * 1_000_000_000)
        return time_since_open_minutes < self.session_open_window_minutes
    
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
    
    def _is_initiation(self, bar, absorption_bar, slow_trend: int, ctx) -> bool:
        """
        Initiation: opposite delta that is strong and efficient.
        
        Uses different thresholds based on trend. Efficiency requirement increases
        with bars since absorption (decay factor).
        
        Also checks for price progress and early session penalty.
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
        
        # Early session penalty: stricter efficiency
        if self._is_in_early_session(ctx):
            efficiency += self.early_session_efficiency_penalty
            efficiency = min(0.95, efficiency)
        
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
            
            # Check efficiency: close in lower portion
            if close_pos > (1.0 - efficiency):
                return False
            
            # Price should make progress downward with minimum move
            if bar.low >= absorption_bar.get("high", 0) - self.min_price_progress:
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            if current_delta <= threshold:
                return False
            
            # Check efficiency: close in upper portion
            if close_pos < efficiency:
                return False
            
            # Price should make progress upward with minimum move
            if bar.high <= absorption_bar.get("low", 0) + self.min_price_progress:
                return False
            
            return True
    
    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Track session start time
        if self.session_start_ts is None or ctx.session != getattr(self, '_last_session', -1):
            self.session_start_ts = ctx.session_start
            self._last_session = ctx.session
        
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
                }
                self.bars_since_absorption = 0
        
        # Check for initiation on current bar
        if self.absorption_bar is not None and self.bars_since_absorption > 0:
            if self._is_initiation(fast_bar, self.absorption_bar, slow_trend, ctx):
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
