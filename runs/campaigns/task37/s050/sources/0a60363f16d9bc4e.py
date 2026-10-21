"""
An order flow strategy with enhanced delta regime detection and adaptive confirmation.

Key insights from trial #79 (score +10.2255, current best):
- Enhanced momentum confirmation with delta acceleration is effective
- Volatility-adjusted delta percentile improves regime-aware filtering
- Discrete asymmetric bracket scaling (vol_trend regimes) remains optimal
- Multi-bar confirmation with price direction alignment is robust
- Smooth momentum adaptation [0.6, 0.5, 0.4] works well

Analysis of peak performers:
- #79: +10.2255 (enhanced momentum + volatility-adjusted percentile)
- #78: +9.9073 (dual-regime confirmation, discrete asymmetric scaling)
- #68: +8.3800 (original asymmetric scaling)
- #72: +8.3328 (with delta acceleration)

Key observation: #79 achieved +10.2255 by combining enhanced momentum
confirmation with volatility-adjusted delta thresholding. The approach
of adapting both confirmation strictness AND threshold calculation to
volatility regime is highly effective.

New hypothesis for improvement over #79:
- #79 is strong at 10.2255 with proven enhanced momentum confirmation
- Current issue: delta acceleration check uses fixed thresholds (-5.0, 5.0)
- Observation: these thresholds don't adapt to delta volatility regime
- Problem: in high volatility sessions, fixed thresholds may be too tight
- New approach: ADAPTIVE DELTA ACCELERATION THRESHOLDS
- Instead of fixed acceleration thresholds, make them volatility-aware:
  - Low vol (vol_ratio < 0.8): tight thresholds (-3.0, 3.0) - strict
  - Normal vol (0.8 <= vol_ratio <= 1.2): moderate (-5.0, 5.0) - baseline
  - High vol (vol_ratio > 1.2): loose (-8.0, 8.0) - permissive
- This allows delta reversals in high vol but catches them in low vol
- Additionally, improve signal strength calculation:
  - Current: signal_strength = abs(current_delta) / adapted_threshold
  - Problem: doesn't account for how strong the momentum is
  - New: weight signal strength by both magnitude AND acceleration
  - signal_strength *= (1.0 + abs(delta_accel) / adapted_threshold)
  - This gives extra weight to building momentum
- Combine with existing confirmation for synergy

This strategy refines #79 by:
1. Keeping core logic from #79:
   - Enhanced momentum confirmation with delta acceleration
   - Volatility-adjusted delta percentile [75%, 70%, 65%]
   - Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85]
   - Dual-regime entry confirmation (strict/balanced/relaxed)
   - Multi-bar confirmation with price direction
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Combined volatility ratio scaling [0.6, 1.8]
   - Smooth momentum adaptation [0.6, 0.5, 0.4]

2. Adding adaptive delta acceleration thresholds:
   - Low vol: tight thresholds (-3.0, 3.0) for strict reversals
   - Normal vol: moderate thresholds (-5.0, 5.0) baseline
   - High vol: loose thresholds (-8.0, 8.0) for permissive entries
   - Adapts acceleration tolerance to volatility regime
   - Synergizes with volatility-adjusted percentile

3. Enhancing signal strength calculation:
   - Weight by both magnitude and acceleration
   - signal_strength *= (1.0 + abs(delta_accel) / adapted_threshold)
   - Gives extra weight to building momentum
   - Improves entry quality on sustained moves

4. Maintaining all proven components:
   - Discrete asymmetric bracket scaling (proven at #68, #78, #79)
   - Enhanced momentum confirmation (proven at #79)
   - Volatility-adjusted delta percentile (proven at #79)
   - Fixed 1.5x signal strength threshold
   - Price direction confirmation
   - Multi-bar confirmation with majority rule

Key hypothesis:
- #79 is strong at 10.2255 with proven enhanced momentum confirmation
- Adaptive acceleration thresholds improve entries in different vol regimes
- Weighting signal strength by acceleration captures momentum builds better
- Combined approach should improve or match #79 by being more regime-aware
- Should capture more high-quality entries while filtering false reversals
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
        confirmation_bars: int = 2,
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
            confirmation_bars: number of bars that must align with trend
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.confirmation_bars = confirmation_bars
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
        self.trend_signals: deque[int] = deque(maxlen=confirmation_bars)
        
        # Track recent price direction for secondary trend confirmation
        self.price_directions: deque[int] = deque(maxlen=3)
        
        # Track previous delta for momentum confirmation
        self.prev_delta: float = 0.0
        
        # Track longer-term volatility for trend detection
        self.longer_ranges: deque[float] = deque(maxlen=vol_lookback * 2)
        
        # Track delta acceleration (current - prev)
        self.prev_prev_delta: float = 0.0
        
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

    def _get_smooth_adaptive_momentum(self, vol_ratio: float) -> float:
        """
        Get momentum threshold adapted to volatility regime with smooth transitions.
        
        vol_ratio < 0.7: 0.6 (strict, low volatility)
        vol_ratio 0.7-0.9: linear interpolate 0.6 → 0.5
        vol_ratio 0.9-1.1: 0.5 (baseline, normal volatility)
        vol_ratio 1.1-1.3: linear interpolate 0.5 → 0.4
        vol_ratio > 1.3: 0.4 (loose, high volatility)
        """
        if vol_ratio < 0.7:
            return 0.6
        elif vol_ratio < 0.9:
            t = (vol_ratio - 0.7) / (0.9 - 0.7)
            return 0.6 - t * 0.1
        elif vol_ratio < 1.1:
            return 0.5
        elif vol_ratio < 1.3:
            t = (vol_ratio - 1.1) / (1.3 - 1.1)
            return 0.5 - t * 0.1
        else:
            return 0.4

    def _get_volatility_adjusted_percentile(self, vol_ratio: float) -> float:
        """
        Get percentile threshold for delta based on volatility regime.
        
        Low vol (vol_ratio < 0.8): 75th percentile (stricter)
        Normal vol (0.8 <= vol_ratio <= 1.2): 70th percentile (baseline)
        High vol (vol_ratio > 1.2): 65th percentile (looser)
        """
        if vol_ratio < 0.8:
            return 0.75
        elif vol_ratio > 1.2:
            return 0.65
        else:
            return 0.70

    def _get_adaptive_accel_thresholds(self, vol_ratio: float) -> tuple[float, float]:
        """
        Get adaptive delta acceleration thresholds based on volatility regime.
        
        Returns (negative_threshold, positive_threshold) for detecting reversals.
        
        Low vol (vol_ratio < 0.8): tight (-3.0, 3.0)
        Normal vol (0.8 <= vol_ratio <= 1.2): moderate (-5.0, 5.0)
        High vol (vol_ratio > 1.2): loose (-8.0, 8.0)
        """
        if vol_ratio < 0.8:
            return -3.0, 3.0
        elif vol_ratio > 1.2:
            return -8.0, 8.0
        else:
            return -5.0, 5.0

    def _check_momentum_with_adaptive_acceleration(
        self, current_delta: float, signal: int, adaptive_threshold: float, vol_ratio: float
    ) -> bool:
        """
        Check if delta momentum aligns with signal direction AND delta is building.
        Uses adaptive acceleration thresholds based on volatility regime.
        """
        if signal == 0:
            return False
        
        # Check base momentum
        if signal > 0:
            momentum_ok = current_delta > adaptive_threshold * self.prev_delta
        else:
            momentum_ok = current_delta < adaptive_threshold * self.prev_delta
        
        if not momentum_ok:
            return False
        
        # Check delta acceleration with adaptive thresholds
        delta_accel = current_delta - self.prev_delta
        neg_thresh, pos_thresh = self._get_adaptive_accel_thresholds(vol_ratio)
        
        if signal > 0:
            # For long, delta should be building upward
            return delta_accel > neg_thresh
        else:
            # For short, delta should be building downward
            return delta_accel < pos_thresh

    def _get_asymmetric_bracket_scaling(self, ranges_list: list[float], longer_ranges_list: list[float]) -> tuple[float, float]:
        """
        Compute asymmetric bracket scaling based on volatility trend (discrete regimes).
        
        Returns (stop_scale, target_scale) multipliers.
        """
        if len(ranges_list) < 2 or len(longer_ranges_list) < 4:
            return 1.0, 1.0
        
        # Recent volatility (last vol_lookback bars)
        recent_vol = sum(ranges_list) / len(ranges_list)
        
        # Longer-term volatility (last 2*vol_lookback bars)
        longer_vol = sum(longer_ranges_list) / len(longer_ranges_list)
        
        if longer_vol <= 0:
            return 1.0, 1.0
        
        vol_trend = recent_vol / longer_vol
        
        # Clamp vol_trend to reasonable range
        vol_trend = max(0.8, min(vol_trend, 1.2))
        
        # Discrete asymmetric scaling based on volatility trend (proven at #68, #78, #79)
        if vol_trend > 1.1:
            # Increasing volatility: tighter stop, wider target
            stop_scale = 0.85
            target_scale = 1.15
        elif vol_trend < 0.9:
            # Decreasing volatility: wider stop, tighter target
            stop_scale = 1.15
            target_scale = 0.85
        else:
            # Normal volatility: balanced scaling
            stop_scale = 1.0
            target_scale = 1.0
        
        return stop_scale, target_scale

    def _get_regime_confirmation_params(self, vol_ratio: float) -> tuple[int, float]:
        """
        Get confirmation requirements based on volatility regime.
        
        Returns (min_conditions_needed, momentum_threshold).
        
        Low volatility (vol_ratio < 0.8): Strict (5 of 5, 0.6x momentum)
        Normal volatility (0.8 <= vol_ratio <= 1.2): Balanced (signal strength logic, 0.5x)
        High volatility (vol_ratio > 1.2): Relaxed (3 of 5, 0.4x momentum)
        """
        if vol_ratio < 0.8:
            # Low volatility: strict confirmation
            return 5, 0.6
        elif vol_ratio > 1.2:
            # High volatility: relaxed confirmation
            return 3, 0.4
        else:
            # Normal volatility: balanced (will be determined by signal strength)
            return -1, 0.5  # -1 indicates to use signal strength logic

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.trend_signals.clear()
            self.price_directions.clear()
            self.longer_ranges.clear()
            self.prev_delta = 0.0
            self.prev_prev_delta = 0.0
        
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
        self.longer_ranges.append(bar_range)
        
        # Track price direction (up=1, down=-1, flat=0)
        price_dir = 0
        if last.close > last.open:
            price_dir = 1
        elif last.close < last.open:
            price_dir = -1
        self.price_directions.append(price_dir)
        
        # Need enough history for moving average
        if len(self.closes) < self.ma_period:
            self.prev_prev_delta = self.prev_delta
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
            self.prev_prev_delta = self.prev_delta
            self.prev_delta = delta
            return
        
        # Compute delta volatility for adaptive thresholding
        deltas_list = list(self.deltas)
        delta_vol = self._compute_delta_volatility(deltas_list)
        
        # Compute volatility adjustment using 65th percentile
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            vol_idx = int(0.65 * (len(ranges_sorted) - 1))
            vol_idx = max(0, min(vol_idx, len(ranges_sorted) - 1))
            percentile_range = ranges_sorted[vol_idx]
            
            avg_range = sum(ranges_list) / len(ranges_list)
            if avg_range > 0.0:
                vol_ratio = percentile_range / avg_range
            else:
                vol_ratio = 1.0
        else:
            vol_ratio = 1.0
        
        # Clamp volatility ratio to reasonable bounds
        vol_ratio = max(0.6, min(vol_ratio, 1.6))
        
        # Get volatility-adjusted percentile for delta threshold
        delta_percentile = self._get_volatility_adjusted_percentile(vol_ratio)
        
        # Compute delta threshold from volatility-adjusted percentile
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        threshold_idx = int(delta_percentile * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
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
        
        # Get smooth adaptive momentum threshold based on volatility regime
        adaptive_momentum = self._get_smooth_adaptive_momentum(vol_ratio)
        
        # Scale bracket levels based on both price volatility and delta volatility
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.6, min(combined_vol_ratio, 1.8))
        
        # Get asymmetric bracket scaling (discrete regimes from #68, #78, #79)
        stop_scale, target_scale = self._get_asymmetric_bracket_scaling(
            list(self.ranges), list(self.longer_ranges)
        )
        
        stop_offset = self.base_stop_offset * combined_vol_ratio * stop_scale
        target_offset = self.base_target_offset * combined_vol_ratio * target_scale
        
        # Get regime-based confirmation parameters
        regime_min_conditions, regime_momentum = self._get_regime_confirmation_params(vol_ratio)
        
        # Signal strength-based confirmation
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Check adaptive momentum confirmation with adaptive acceleration
            momentum_ok = self._check_momentum_with_adaptive_acceleration(
                current_delta, signal, regime_momentum, vol_ratio
            )
            
            # Compute delta acceleration
            delta_accel = current_delta - self.prev_delta
            
            # Compute signal strength with acceleration weighting
            if adapted_threshold > 0:
                signal_strength = abs(current_delta) / adapted_threshold
                # Weight by acceleration: stronger momentum builds get extra weight
                accel_weight = 1.0 + min(abs(delta_accel) / adapted_threshold, 0.5)
                signal_strength *= accel_weight
            else:
                signal_strength = 1.0
            
            # Determine confirmation requirements based on regime
            if regime_min_conditions == -1:
                # Normal volatility: use signal strength logic (proven from #68, #78, #79)
                is_strong_signal = signal_strength >= 1.5
                min_conditions = 4 if is_strong_signal else 5
            else:
                # Low or high volatility: use regime-specific requirements
                min_conditions = regime_min_conditions
            
            # Long entry
            if signal > 0:
                long_conditions_met = 0
                
                if price_above_ma:
                    long_conditions_met += 1
                if positive_signals >= self.confirmation_bars - 1:
                    long_conditions_met += 1
                if signal > 0:
                    long_conditions_met += 1
                if positive_price_dirs >= len(price_dirs_list) - 1:
                    long_conditions_met += 1
                if momentum_ok:
                    long_conditions_met += 1
                
                if long_conditions_met >= min_conditions:
                    ctx.buy(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="entry",
                    )
            
            # Short entry
            elif signal < 0:
                short_conditions_met = 0
                
                if price_below_ma:
                    short_conditions_met += 1
                if negative_signals >= self.confirmation_bars - 1:
                    short_conditions_met += 1
                if signal < 0:
                    short_conditions_met += 1
                if negative_price_dirs >= len(price_dirs_list) - 1:
                    short_conditions_met += 1
                if momentum_ok:
                    short_conditions_met += 1
                
                if short_conditions_met >= min_conditions:
                    ctx.sell(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="entry",
                    )
        
        # Update previous deltas for next bar
        self.prev_prev_delta = self.prev_delta
        self.prev_delta = current_delta

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
