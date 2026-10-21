"""
An order flow strategy with refined delta confirmation through volatility-aware thresholding.

Key insights from prior trials:

1. Trial #36 (score +5.8422) - BEST OVERALL:
   - Soft delta momentum confirmation with 0.5x threshold
   - Adaptive delta threshold from 70th percentile
   - Combined volatility ratio scaling [0.6, 1.8]
   - Multi-bar confirmation with price direction alignment
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #42 (score +2.4073):
   - Signal strength weighting added complexity
   - min_strength_sum threshold of 0.8 was too restrictive
   - Reduced trade count without improving quality
   - Score dropped significantly from #36

3. Trial #41 (score NOT SCORED - exposed in 71 of 105 sessions):
   - Delta phase detection was too restrictive
   - Early phase filter eliminated too many valid entries
   - Reduced trade count below 70% minimum threshold

4. Analysis of what works best:
   - #36's fixed parameters are near-optimal at 5.8422
   - The core formula is robust and simple
   - Adding restrictive filters or complex weighting degrades performance
   - Over-engineering consistently fails; simplicity wins

5. New hypothesis for improvement over #36:
   - #36 is strong at 5.8422 - keep the core formula
   - The opportunity is in THRESHOLD CALIBRATION STABILITY
   - Current approach: uses fixed 70th percentile on absolute deltas
   - Problem: percentile ranking is discrete, creating jumps at boundaries
   - New approach: use SMOOTHED percentile with interpolation
   - Smooth delta threshold across regimes instead of discrete jumps
   - Add VOLATILITY REGIME DETECTION to adjust confirmation strictness
   - In low vol: keep confirmation strict (more confirmation bars needed)
   - In high vol: relax confirmation (fewer bars needed due to noise)
   - This adapts to market conditions without adding new filters

6. Smoothed threshold and regime approach:
   - Interpolate percentile rank instead of using discrete index
   - Use linear interpolation between sorted values
   - Detect volatility regime: low/medium/high based on delta_vol
   - Adjust confirmation_bars requirement based on regime
   - Low vol (delta_vol < 40): require 2 bars (strict)
   - Medium vol (40-60): require 2 bars (baseline)
   - High vol (delta_vol > 60): require 1 bar (relaxed due to noise)
   - This maintains trade count while improving quality in high vol

This strategy refines #36 by:
1. Keeping all core logic from #36 exactly:
   - 70th percentile delta threshold (but with smoothing)
   - Adaptive delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8]
   - MA period 10, delta lookback 14, vol lookback 12
   - Price direction confirmation
   - Fixed 0.5x momentum threshold
   - Multi-bar confirmation

2. Adding smoothed percentile calculation:
   - Use linear interpolation on sorted deltas
   - Removes discrete jumps in threshold
   - Creates smooth transitions across volatility regimes
   - More stable threshold behavior

3. Adding volatility regime detection:
   - Measure delta volatility (standard deviation)
   - Adjust confirmation bar requirement based on regime
   - High vol: relax confirmation (1 bar sufficient)
   - Low vol: keep strict (2 bars required)
   - Adapts to market conditions naturally

4. Maintaining all proven parameters:
   - MA period 10, delta lookback 14, vol lookback 12
   - Base stop/target offset 0.75
   - Combined vol ratio [0.6, 1.8]
   - 70th percentile delta threshold (smoothed)
   - Fixed 0.5x momentum threshold

Key hypothesis:
- #36 is strong at 5.8422
- Smoothed percentile improves threshold stability
- Volatility regime detection adapts confirmation naturally
- No new filters, just smoother and more adaptive logic
- Should improve on #36 by handling edge cases better
- Maintains simplicity while improving robustness
"""

from __future__ import annotations
from collections import deque
import math


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        ma_period: int = 10,
        delta_lookback: int = 14,
        vol_lookback: int = 12,
        momentum_threshold: float = 0.5,
        size: int = 1,
        base_stop_offset: float = 0.75,
        base_target_offset: float = 0.75,
    ):
        """
        Args:
            interval: bar interval to subscribe to
            ma_period: period for moving average (trend filter)
            delta_lookback: bars to measure delta distribution over
            vol_lookback: bars for volatility measurement
            momentum_threshold: multiplier for momentum check
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.momentum_threshold = momentum_threshold
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
        # Track closes for moving average
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track deltas for distribution analysis
        self.deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track bar ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track trend signals for multi-bar confirmation
        self.trend_signals: deque[int] = deque(maxlen=2)
        
        # Track recent price direction for secondary trend confirmation
        self.price_directions: deque[int] = deque(maxlen=3)
        
        # Track previous delta for momentum confirmation
        self.prev_delta: float = 0.0
        
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _compute_delta_volatility(self, deltas_list: list[float]) -> float:
        """Compute the standard deviation of deltas."""
        if len(deltas_list) < 2:
            return 1.0
        
        mean_delta = sum(deltas_list) / len(deltas_list)
        variance = sum((d - mean_delta) ** 2 for d in deltas_list) / len(deltas_list)
        
        if variance <= 0:
            return 1.0
        
        return math.sqrt(variance)

    def _get_smoothed_percentile(self, sorted_values: list[float], percentile: float) -> float:
        """
        Get percentile value with linear interpolation for smoothness.
        Prevents discrete jumps in threshold.
        """
        if not sorted_values:
            return 0.0
        
        if len(sorted_values) == 1:
            return sorted_values[0]
        
        # Calculate exact position (may be fractional)
        position = percentile * (len(sorted_values) - 1)
        lower_idx = int(position)
        upper_idx = min(lower_idx + 1, len(sorted_values) - 1)
        
        # Linear interpolation between the two nearest values
        if lower_idx == upper_idx:
            return sorted_values[lower_idx]
        
        fraction = position - lower_idx
        lower_val = sorted_values[lower_idx]
        upper_val = sorted_values[upper_idx]
        
        return lower_val + fraction * (upper_val - lower_val)

    def _get_required_confirmation_bars(self, delta_vol: float) -> int:
        """
        Determine required confirmation bars based on volatility regime.
        High vol: relax confirmation (1 bar)
        Low vol: keep strict (2 bars)
        """
        if delta_vol > 60.0:
            return 1  # High volatility: relax confirmation
        elif delta_vol < 40.0:
            return 2  # Low volatility: strict confirmation
        else:
            return 2  # Medium volatility: baseline

    def _check_momentum(self, current_delta: float, signal: int) -> bool:
        """
        Check if delta momentum aligns with signal direction.
        Fixed momentum threshold at 0.5x (proven in #36).
        """
        if signal == 0:
            return False
        
        if signal > 0:
            # Buy signal: require current_delta > 0.5 * previous_delta
            return current_delta > self.momentum_threshold * self.prev_delta
        else:
            # Sell signal: require current_delta < 0.5 * previous_delta
            return current_delta < self.momentum_threshold * self.prev_delta

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.trend_signals.clear()
            self.price_directions.clear()
            self.prev_delta = 0.0
        
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last = history[-1]
        
        # Accumulate data
        self.closes.append(last.close)
        delta = float(last.buy_volume - last.sell_volume)
        self.deltas.append(delta)
        bar_range = last.high - last.low
        self.ranges.append(bar_range)
        
        # Track price direction (up=1, down=-1, flat=0)
        price_dir = 0
        if last.close > last.open:
            price_dir = 1
        elif last.close < last.open:
            price_dir = -1
        self.price_directions.append(price_dir)
        
        # Need enough history for moving average
        if len(self.closes) < self.ma_period:
            self.prev_delta = delta
            return
        
        # Compute moving average (trend filter)
        closes_list = list(self.closes)
        ma = sum(closes_list) / len(closes_list)
        current_price = closes_list[-1]
        
        # Determine price trend direction
        price_above_ma = current_price > ma
        price_below_ma = current_price < ma
        
        # Need enough delta history to compute threshold
        if len(self.deltas) < self.delta_lookback:
            self.prev_delta = delta
            return
        
        # Compute delta volatility for adaptive thresholding
        deltas_list = list(self.deltas)
        delta_vol = self._compute_delta_volatility(deltas_list)
        
        # Compute delta threshold from smoothed percentile of absolute deltas
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # 70th percentile for threshold (smoothed with interpolation)
        delta_threshold = self._get_smoothed_percentile(abs_deltas_sorted, 0.70)
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Adapt threshold based on delta volatility
        delta_vol_ratio = max(0.7, min(delta_vol / 50.0, 1.3))
        adapted_threshold = delta_threshold * delta_vol_ratio
        
        # Current delta
        current_delta = delta
        
        # Determine signal based on delta extremeness
        signal = 0
        if current_delta > adapted_threshold:
            signal = 1  # Strong buy signal
        elif current_delta < -adapted_threshold:
            signal = -1  # Strong sell signal
        
        self.trend_signals.append(signal)
        
        # Compute volatility adjustment using 65th percentile (like #26)
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            percentile_range = self._get_smoothed_percentile(ranges_sorted, 0.65)
            
            avg_range = sum(ranges_list) / len(ranges_list)
            if avg_range > 0.0:
                vol_ratio = percentile_range / avg_range
            else:
                vol_ratio = 1.0
        else:
            vol_ratio = 1.0
        
        # Clamp volatility ratio to reasonable bounds
        vol_ratio = max(0.6, min(vol_ratio, 1.6))
        
        # Scale bracket levels based on both price volatility and delta volatility
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.6, min(combined_vol_ratio, 1.8))
        
        stop_offset = self.base_stop_offset * combined_vol_ratio
        target_offset = self.base_target_offset * combined_vol_ratio
        
        # Get required confirmation bars based on volatility regime
        required_confirmation = self._get_required_confirmation_bars(delta_vol)
        
        # Multi-bar confirmation with regime-aware strictness
        if len(self.trend_signals) >= required_confirmation:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Check momentum confirmation
            momentum_ok = self._check_momentum(current_delta, signal)
            
            # Long: price above MA, positive signals meet requirement,
            # current signal positive, recent price action shows upward bias, momentum confirms
            if (price_above_ma and 
                positive_signals >= required_confirmation and
                signal > 0 and
                positive_price_dirs >= len(price_dirs_list) - 1 and
                momentum_ok):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
            
            # Short: price below MA, negative signals meet requirement,
            # current signal negative, recent price action shows downward bias, momentum confirms
            elif (price_below_ma and 
                  negative_signals >= required_confirmation and
                  signal < 0 and
                  negative_price_dirs >= len(price_dirs_list) - 1 and
                  momentum_ok):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
        
        # Update previous delta for next bar
        self.prev_delta = current_delta

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
