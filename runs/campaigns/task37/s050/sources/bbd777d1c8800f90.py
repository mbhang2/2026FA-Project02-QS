"""
An order flow strategy with refined signal strength calculation and improved momentum confirmation.

Key insights from trial #80 (score +10.2255, best performer):
- Adaptive acceleration thresholds per volatility regime are highly effective
- Signal strength >= 1.5 threshold with acceleration weighting works well
- Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85] is optimal
- Volatility-adjusted delta percentile [75%, 70%, 65%] is robust
- Smooth momentum adaptation [0.6, 0.5, 0.4] is proven

Analysis of recent trials:
- #80: +10.2255 (best: adaptive accel thresholds, signal strength weighting)
- #79: +10.2255 (enhanced momentum + volatility-adjusted percentile)
- #78: +9.9073 (dual-regime confirmation, discrete asymmetric scaling)
- #82: +6.2866 (weighted percentile, smoothed momentum - dropped)
- #83: +6.0924 (delta trend detection, directional momentum - dropped)

Key observation: Trials #82 and #83 both dropped significantly despite
theoretical improvements. This suggests the core logic from #80 is near-optimal,
and modifications are degrading performance. The issue appears to be in how
momentum confirmation is being checked.

New hypothesis for improvement over #80:
- #80 is strong at 10.2255 with proven adaptive acceleration thresholds
- #82 and #83 dropped because momentum checks became too lenient/strict
- Problem: momentum threshold uses regime_momentum which varies [0.4-0.6]
- Observation: this threshold may be too variable across regimes
- New approach: STABILIZED MOMENTUM WITH BETTER SIGNAL QUALITY
- Instead of regime-dependent momentum thresholds, use fixed threshold
- Keep regime-dependent acceleration thresholds (proven from #80)
- Momentum check: require current_delta > 0.5 * prev_delta (fixed)
- Acceleration check: use regime-adaptive thresholds (proven from #80)
- This separates momentum detection from regime adaptation
- Additionally, refine signal strength calculation:
  - Current: signal_strength = (abs(current_delta) / adapted_threshold) * (1 + min(accel_weight, 0.5))
  - Problem: accel_weight can be large, making signal_strength unstable
  - New: cap accel_weight more aggressively
  - Use min(abs(delta_accel) / adapted_threshold, 0.3) instead of 0.5
  - This prevents acceleration from dominating signal strength
- Keep all other proven components:
  - Discrete asymmetric bracket scaling
  - Volatility-adjusted delta percentile
  - Multi-bar confirmation with price direction
  - Fixed confirmation_bars=2 and signal_strength >= 1.5

This strategy refines #80 by:
1. Keeping all proven core from #80:
   - Adaptive acceleration thresholds per volatility regime
   - Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85]
   - Volatility-adjusted delta percentile [75%, 70%, 65%]
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Combined volatility ratio scaling [0.6, 1.8]
   - Smooth momentum adaptation [0.6, 0.5, 0.4]
   - Fixed confirmation_bars=2 and signal_strength >= 1.5

2. Stabilizing momentum confirmation:
   - Use fixed momentum threshold (0.5x prev_delta)
   - Keep regime-adaptive acceleration thresholds
   - Separates momentum from regime adaptation
   - More stable across different volatility regimes

3. Refining signal strength calculation:
   - Cap acceleration weight at 0.3 instead of 0.5
   - Prevents acceleration from destabilizing signal strength
   - Makes threshold more consistent
   - Better filtering of marginal signals

Key hypothesis:
- #80 is strong at 10.2255 with proven adaptive acceleration thresholds
- Momentum threshold should be fixed, not regime-dependent
- Acceleration weight cap should be tighter (0.3 vs 0.5)
- Should match or exceed #80 by stabilizing momentum detection
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

    def _check_momentum_with_fixed_threshold(
        self, current_delta: float, signal: int, vol_ratio: float
    ) -> bool:
        """
        Check if delta momentum aligns with signal direction using fixed threshold.
        
        Uses fixed 0.5x momentum threshold independent of volatility regime.
        Acceleration is checked with regime-adaptive thresholds.
        """
        if signal == 0:
            return False
        
        # Fixed momentum threshold (0.5x prev_delta)
        fixed_momentum_threshold = 0.5
        
        # Check base momentum with fixed threshold
        if signal > 0:
            momentum_ok = current_delta > fixed_momentum_threshold * self.prev_delta
        else:
            momentum_ok = current_delta < fixed_momentum_threshold * self.prev_delta
        
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
            
            # Check momentum confirmation with fixed threshold
            momentum_ok = self._check_momentum_with_fixed_threshold(current_delta, signal, vol_ratio)
            
            # Compute delta acceleration
            delta_accel = current_delta - self.prev_delta
            
            # Compute signal strength with tighter acceleration weight cap
            if adapted_threshold > 0:
                signal_strength = abs(current_delta) / adapted_threshold
                # Cap acceleration weight at 0.3 (tighter than #80's 0.5)
                accel_weight = 1.0 + min(abs(delta_accel) / adapted_threshold, 0.3)
                signal_strength *= accel_weight
            else:
                signal_strength = 1.0
            
            # Determine confirmation requirements (fixed from #80)
            is_strong_signal = signal_strength >= 1.5
            min_conditions = 4 if is_strong_signal else 5
            
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
