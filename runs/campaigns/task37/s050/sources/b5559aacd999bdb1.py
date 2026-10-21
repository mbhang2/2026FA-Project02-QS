"""
An order flow strategy with refined signal detection and improved entry confirmation.

Key insights from trial #80 (score +10.2255, best performer):
- Adaptive acceleration thresholds per volatility regime are highly effective
- Signal strength >= 1.5 threshold works well
- Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85] is optimal
- Volatility-adjusted delta percentile [75%, 70%, 65%] is robust

Analysis of recent trials:
- #80: +10.2255 (best: adaptive accel thresholds, signal strength weighting)
- #79: +10.2255 (enhanced momentum + volatility-adjusted percentile)
- #89: +3.4229 (directional thresholds, minimum prev_delta check)
- #88: +5.2334 (stabilized momentum bonus, hard signal >= 1.5)

Key observation: The gap from #80 to #89 suggests that directional thresholds
and minimum prev_delta checks hurt entry quality. The original approach of
using a single percentile-based threshold appears more robust.

New hypothesis for improvement over #80:
- #80 is strong at 10.2255 with proven components
- Problem: current momentum confirmation uses single prev_delta comparison
- Observation: single-bar momentum can be noisy
- New approach: IMPROVED MOMENTUM CONFIRMATION WITH MULTI-BAR CONTEXT
- Keep all proven components from #80:
  - Adaptive acceleration thresholds: -3/-5/-8 per volatility regime
  - Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85]
  - Volatility-adjusted delta percentile [75%, 70%, 65%]
  - Regime-adaptive momentum threshold [0.4, 0.5, 0.6]
  - Signal strength >= 1.5 threshold (proven at #80)
  - Fixed confirmation_bars=2
- Improve momentum confirmation:
  - Current: requires momentum_threshold * prev_delta comparison
  - Problem: single prev_delta can be noisy
  - New: add secondary momentum check using delta trend
  - Check if current_delta is building in the right direction
  - Require EITHER momentum check OR delta building trend
  - This provides alternative path for momentum confirmation
  - More robust than single-bar momentum comparison
- Additionally, refine acceleration threshold bounds:
  - Current: fixed per regime (-3/-5/-8, +3/+5/+8)
  - Observation: bounds could be tighter for better filtering
  - New: scale acceleration bounds based on delta volatility
  - Higher delta_vol_ratio -> wider bounds (more tolerance)
  - Lower delta_vol_ratio -> tighter bounds (stricter filtering)
  - This adapts acceleration check to market conditions
- Keep all other proven components:
  - MA period 10, delta_lookback 14, vol_lookback 12
  - Combined volatility ratio scaling [0.6, 1.8]
  - Single percentile-based delta threshold (proven at #80)
  - No directional thresholds (revert from #89)

This strategy refines #80 by:
1. Keeping all proven core from #80:
   - Adaptive acceleration thresholds: -3/-5/-8 (proven optimal)
   - Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85]
   - Volatility-adjusted delta percentile [75%, 70%, 65%]
   - Regime-adaptive momentum threshold [0.4, 0.5, 0.6]
   - Signal strength >= 1.5 threshold (proven at #80)
   - Fixed confirmation_bars=2
   - Single percentile-based delta threshold

2. Improving momentum confirmation:
   - Add secondary momentum check: delta building trend
   - Require EITHER primary momentum OR delta building
   - More robust than single-bar momentum comparison
   - Provides alternative path for momentum confirmation

3. Refining acceleration threshold bounds:
   - Scale bounds based on delta_vol_ratio
   - Higher vol -> wider bounds (more tolerance)
   - Lower vol -> tighter bounds (stricter filtering)
   - Adapts acceleration check to market conditions

Key hypothesis:
- #80 is strong at 10.2255 with proven adaptive thresholds
- Secondary momentum check improves confirmation robustness
- Scaled acceleration bounds adapt to market volatility
- Should match or exceed #80 by improving momentum confirmation
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

    def _get_volatility_adjusted_percentile(self, vol_ratio: float) -> float:
        """Get percentile threshold for delta based on volatility regime."""
        if vol_ratio < 0.8:
            return 0.75
        elif vol_ratio > 1.2:
            return 0.65
        else:
            return 0.70

    def _get_regime_momentum_threshold(self, vol_ratio: float) -> float:
        """
        Get momentum threshold adapted to volatility regime.
        
        Low vol (vol_ratio < 0.8): 0.6x (stricter)
        Normal vol (0.8 <= vol_ratio <= 1.2): 0.5x (baseline)
        High vol (vol_ratio > 1.2): 0.4x (looser)
        """
        if vol_ratio < 0.8:
            return 0.6
        elif vol_ratio > 1.2:
            return 0.4
        else:
            return 0.5

    def _get_adaptive_accel_thresholds(self, vol_ratio: float, delta_vol_ratio: float) -> tuple[float, float]:
        """
        Get adaptive delta acceleration thresholds based on volatility regime.
        Scales bounds based on delta volatility.
        """
        # Base thresholds per regime
        if vol_ratio < 0.8:
            base_neg, base_pos = -3.0, 3.0
        elif vol_ratio > 1.2:
            base_neg, base_pos = -8.0, 8.0
        else:
            base_neg, base_pos = -5.0, 5.0
        
        # Scale based on delta volatility
        scale = max(0.7, min(delta_vol_ratio, 1.3))
        
        return base_neg * scale, base_pos * scale

    def _check_momentum_with_accel(
        self, current_delta: float, signal: int, vol_ratio: float, delta_vol_ratio: float
    ) -> bool:
        """
        Check if delta momentum aligns with signal direction using regime-adaptive threshold.
        Uses volatility-adaptive momentum threshold and checks acceleration within bounds.
        Also checks secondary momentum via delta building trend.
        """
        if signal == 0:
            return False
        
        # Get regime-adaptive momentum threshold
        momentum_threshold = self._get_regime_momentum_threshold(vol_ratio)
        
        # Check base momentum
        primary_momentum_ok = False
        if signal > 0:
            primary_momentum_ok = current_delta > momentum_threshold * self.prev_delta
        else:
            primary_momentum_ok = current_delta < momentum_threshold * self.prev_delta
        
        # Check delta acceleration with scaled adaptive thresholds
        delta_accel = current_delta - self.prev_delta
        neg_thresh, pos_thresh = self._get_adaptive_accel_thresholds(vol_ratio, delta_vol_ratio)
        
        accel_ok = False
        if signal > 0:
            accel_ok = delta_accel > neg_thresh
        else:
            accel_ok = delta_accel < pos_thresh
        
        # Secondary momentum check: delta building trend
        # For long: check if delta is consistently positive/building
        # For short: check if delta is consistently negative/building
        secondary_momentum_ok = False
        if signal > 0:
            # For long, require current delta to be positive (building upward)
            secondary_momentum_ok = current_delta > 0 and abs(current_delta) > 0.5
        else:
            # For short, require current delta to be negative (building downward)
            secondary_momentum_ok = current_delta < 0 and abs(current_delta) > 0.5
        
        # Require EITHER primary momentum OR (acceleration AND secondary momentum)
        return primary_momentum_ok or (accel_ok and secondary_momentum_ok)

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
        
        # Get volatility-adjusted percentile for delta threshold
        delta_percentile = self._get_volatility_adjusted_percentile(vol_ratio)
        
        # Compute delta threshold from percentile
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
        
        # Determine signal based on delta extremeness
        signal = 0
        if current_delta > adapted_threshold:
            signal = 1
        elif current_delta < -adapted_threshold:
            signal = -1
        
        self.trend_signals.append(signal)
        
        # Scale bracket levels
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.6, min(combined_vol_ratio, 1.8))
        
        # Get asymmetric bracket scaling
        stop_scale, target_scale = self._get_asymmetric_bracket_scaling(
            list(self.ranges), list(self.longer_ranges)
        )
        
        stop_offset = self.base_stop_offset * combined_vol_ratio * stop_scale
        target_offset = self.base_target_offset * combined_vol_ratio * target_scale
        
        # Signal strength-based confirmation
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Check momentum confirmation with adaptive thresholds
            momentum_ok = self._check_momentum_with_accel(
                current_delta, signal, vol_ratio, delta_vol_ratio
            )
            
            # Compute signal strength
            if adapted_threshold > 0:
                signal_strength = abs(current_delta) / adapted_threshold
            else:
                signal_strength = 1.0
            
            # Hard signal_strength >= 1.5 threshold (proven at #80)
            is_strong_signal = signal_strength >= 1.5
            
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
                
                if is_strong_signal and long_conditions_met >= 4:
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
                
                if is_strong_signal and short_conditions_met >= 4:
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
