"""
An order flow strategy with refined signal filtering and adaptive entry thresholds.

Key insights from trial #82 (score +6.2866, recent):
- Weighted percentile calculation improves threshold responsiveness
- Smoothed 2-bar momentum reduces single-bar noise
- Fixed confirmation_bars=2 with signal_strength >= 1.5 is more stable than adaptive
- Discrete asymmetric bracket scaling remains optimal
- Volatility-adjusted delta percentile works well

Analysis of peak performers:
- #80: +10.2255 (adaptive acceleration thresholds, signal strength weighting)
- #79: +10.2255 (enhanced momentum + volatility-adjusted percentile)
- #78: +9.9073 (dual-regime confirmation, discrete asymmetric scaling)
- #82: +6.2866 (weighted percentile, smoothed momentum)

Key observation: Trial #82 dropped significantly from #80 despite having
better theoretical improvements. This suggests the smoothed 2-bar momentum
check may be too lenient or the weighted percentile may be over-responsive.

New hypothesis for improvement over #80:
- #80 is strong at 10.2255 with proven components
- #82 dropped to 6.28, suggesting smoothed momentum was too permissive
- Problem: (current + prev) / 2 momentum check misses reversals
- New approach: REFINED MOMENTUM WITH BETTER SIGNAL QUALITY
- Instead of smoothed 2-bar momentum, use directional consistency check:
  - For long: require current_delta > prev_delta (not smoothed average)
  - For short: require current_delta < prev_delta (not smoothed average)
  - This is simpler and captures actual directional momentum
- Additionally, improve the delta signal itself:
  - Current: single threshold for all cases
  - Problem: threshold doesn't account for recent delta magnitude trend
  - New: use recent max/min deltas to set dynamic range
  - If recent deltas are building, use lower percentile (65th)
  - If recent deltas are declining, use higher percentile (75th)
  - This makes threshold adapt to delta trend, not just volatility
- Keep the weighted percentile but refine its application:
  - Use it only when delta trend is uncertain
  - When delta trend is clear, use fixed percentile
- Combine with proven components for synergy

This strategy refines #80 by:
1. Keeping proven core from #80:
   - Fixed confirmation_bars=2
   - Signal strength >= 1.5 threshold
   - Adaptive acceleration thresholds per volatility regime
   - Volatility-adjusted delta percentile [75%, 70%, 65%]
   - Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85]
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Combined volatility ratio scaling [0.6, 1.8]
   - Smooth momentum adaptation [0.6, 0.5, 0.4]

2. Reverting momentum check to directional consistency:
   - For long: require current_delta > prev_delta
   - For short: require current_delta < prev_delta
   - Simpler and more robust than smoothed average
   - Captures actual directional momentum better

3. Adding delta trend detection:
   - Track if recent deltas are building or declining
   - Compare recent max vs older max, recent min vs older min
   - If building: use lower percentile (more permissive)
   - If declining: use higher percentile (more conservative)
   - Makes threshold adaptive to delta trend

4. Maintaining all proven components:
   - Discrete asymmetric bracket scaling (proven at #68, #78, #79, #80)
   - Adaptive acceleration thresholds (proven at #80)
   - Dual-regime entry confirmation (proven at #78, #79, #80)
   - Multi-bar confirmation with price direction
   - Fixed signal strength >= 1.5 threshold

Key hypothesis:
- #80 is strong at 10.2255 with proven adaptive acceleration thresholds
- #82 dropped because smoothed momentum was too permissive
- Reverting to directional consistency momentum improves signal quality
- Delta trend detection makes threshold more responsive without being noisy
- Should match or exceed #80 by improving momentum detection
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
        
        # Track older deltas for trend detection (first half of lookback)
        self.older_deltas: deque[float] = deque(maxlen=delta_lookback // 2)
        
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
        """Get momentum threshold adapted to volatility regime with smooth transitions."""
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
        """Get percentile threshold for delta based on volatility regime."""
        if vol_ratio < 0.8:
            return 0.75
        elif vol_ratio > 1.2:
            return 0.65
        else:
            return 0.70

    def _get_adaptive_accel_thresholds(self, vol_ratio: float) -> tuple[float, float]:
        """Get adaptive delta acceleration thresholds based on volatility regime."""
        if vol_ratio < 0.8:
            return -3.0, 3.0
        elif vol_ratio > 1.2:
            return -8.0, 8.0
        else:
            return -5.0, 5.0

    def _detect_delta_trend(self, deltas_list: list[float], older_deltas_list: list[float]) -> str:
        """
        Detect if deltas are building or declining.
        
        Returns "building", "declining", or "neutral".
        """
        if len(deltas_list) < 4 or len(older_deltas_list) < 4:
            return "neutral"
        
        # Recent deltas max/min
        recent_max = max(abs(d) for d in deltas_list[-4:])
        recent_min = min(abs(d) for d in deltas_list[-4:])
        recent_avg = sum(abs(d) for d in deltas_list[-4:]) / 4.0
        
        # Older deltas max/min
        older_max = max(abs(d) for d in older_deltas_list[-4:])
        older_min = min(abs(d) for d in older_deltas_list[-4:])
        older_avg = sum(abs(d) for d in older_deltas_list[-4:]) / 4.0
        
        # Compare trends
        if recent_avg > older_avg * 1.1:
            return "building"
        elif recent_avg < older_avg * 0.9:
            return "declining"
        else:
            return "neutral"

    def _get_delta_trend_adjusted_percentile(self, vol_ratio: float, delta_trend: str) -> float:
        """
        Get percentile adjusted for delta trend.
        
        Building trend: use lower percentile (more permissive)
        Declining trend: use higher percentile (more conservative)
        Neutral: use volatility-adjusted percentile
        """
        base_percentile = self._get_volatility_adjusted_percentile(vol_ratio)
        
        if delta_trend == "building":
            # More permissive when deltas are building
            return base_percentile - 0.05
        elif delta_trend == "declining":
            # More conservative when deltas are declining
            return base_percentile + 0.05
        else:
            return base_percentile

    def _check_directional_momentum(
        self, current_delta: float, signal: int, adaptive_threshold: float, vol_ratio: float
    ) -> bool:
        """
        Check if delta momentum aligns with signal direction using directional consistency.
        
        For long: requires current_delta > prev_delta
        For short: requires current_delta < prev_delta
        """
        if signal == 0:
            return False
        
        # Check directional consistency
        if signal > 0:
            momentum_ok = current_delta > self.prev_delta
        else:
            momentum_ok = current_delta < self.prev_delta
        
        if not momentum_ok:
            return False
        
        # Check delta acceleration with adaptive thresholds
        delta_accel = current_delta - self.prev_delta
        neg_thresh, pos_thresh = self._get_adaptive_accel_thresholds(vol_ratio)
        
        if signal > 0:
            return delta_accel > neg_thresh
        else:
            return delta_accel < pos_thresh

    def _get_asymmetric_bracket_scaling(self, ranges_list: list[float], longer_ranges_list: list[float]) -> tuple[float, float]:
        """Compute asymmetric bracket scaling based on volatility trend."""
        if len(ranges_list) < 2 or len(longer_ranges_list) < 4:
            return 1.0, 1.0
        
        recent_vol = sum(ranges_list) / len(ranges_list)
        longer_vol = sum(longer_ranges_list) / len(longer_ranges_list)
        
        if longer_vol <= 0:
            return 1.0, 1.0
        
        vol_trend = recent_vol / longer_vol
        vol_trend = max(0.8, min(vol_trend, 1.2))
        
        if vol_trend > 1.1:
            stop_scale = 0.85
            target_scale = 1.15
        elif vol_trend < 0.9:
            stop_scale = 1.15
            target_scale = 0.85
        else:
            stop_scale = 1.0
            target_scale = 1.0
        
        return stop_scale, target_scale

    def _get_regime_confirmation_params(self, vol_ratio: float) -> tuple[int, float]:
        """Get confirmation requirements based on volatility regime."""
        if vol_ratio < 0.8:
            return 5, 0.6
        elif vol_ratio > 1.2:
            return 3, 0.4
        else:
            return -1, 0.5

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
            self.older_deltas.clear()
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
        self.longer_ranges.append(bar_range)
        
        # Track older deltas for trend detection
        if len(self.deltas) > self.delta_lookback // 2:
            self.older_deltas.append(delta)
        
        # Track price direction
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
        
        # Compute moving average
        closes_list = list(self.closes)
        ma = sum(closes_list) / len(closes_list)
        current_price = closes_list[-1]
        
        price_above_ma = current_price > ma
        price_below_ma = current_price < ma
        
        # Need enough delta history
        if len(self.deltas) < self.delta_lookback:
            self.prev_delta = delta
            return
        
        # Compute delta volatility
        deltas_list = list(self.deltas)
        delta_vol = self._compute_delta_volatility(deltas_list)
        
        # Compute volatility adjustment
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
        
        vol_ratio = max(0.6, min(vol_ratio, 1.6))
        
        # Detect delta trend
        older_deltas_list = list(self.older_deltas)
        delta_trend = self._detect_delta_trend(deltas_list, older_deltas_list)
        
        # Get trend-adjusted percentile
        delta_percentile = self._get_delta_trend_adjusted_percentile(vol_ratio, delta_trend)
        delta_percentile = max(0.60, min(delta_percentile, 0.80))
        
        # Compute delta threshold
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        threshold_idx = int(delta_percentile * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Adapt threshold based on delta volatility
        delta_vol_ratio = max(0.7, min(delta_vol / 50.0, 1.3))
        adapted_threshold = delta_threshold * delta_vol_ratio
        
        # Current delta
        current_delta = delta
        
        # Determine signal
        signal = 0
        if current_delta > adapted_threshold:
            signal = 1
        elif current_delta < -adapted_threshold:
            signal = -1
        
        self.trend_signals.append(signal)
        
        # Get adaptive momentum threshold
        adaptive_momentum = self._get_smooth_adaptive_momentum(vol_ratio)
        
        # Scale bracket levels
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.6, min(combined_vol_ratio, 1.8))
        
        # Get asymmetric bracket scaling
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
            
            # Secondary trend confirmation
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Check directional momentum confirmation
            momentum_ok = self._check_directional_momentum(
                current_delta, signal, regime_momentum, vol_ratio
            )
            
            # Compute delta acceleration
            delta_accel = current_delta - self.prev_delta
            
            # Compute signal strength with acceleration weighting (proven from #80)
            if adapted_threshold > 0:
                signal_strength = abs(current_delta) / adapted_threshold
                # Weight by acceleration with fixed 0.5 cap (proven from #80)
                accel_weight = 1.0 + min(abs(delta_accel) / adapted_threshold, 0.5)
                signal_strength *= accel_weight
            else:
                signal_strength = 1.0
            
            # Determine confirmation requirements (fixed from #80)
            if regime_min_conditions == -1:
                # Normal volatility: use signal strength logic (proven from #80)
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
        
        # Update previous delta for next bar
        self.prev_delta = current_delta

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
