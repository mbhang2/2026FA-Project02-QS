"""
An order flow strategy with refined delta regime detection and enhanced entry filtering.

Key insights from trial #81 (score +7.7820, recent):
- Momentum-aware signal strength scaling is effective
- Regime-adaptive confirmation bars improve entry quality
- Discrete asymmetric bracket scaling remains optimal
- Volatility-adjusted delta percentile works well
- Smooth momentum adaptation [0.6, 0.5, 0.4] is robust

Analysis of peak performers:
- #80: +10.2255 (adaptive acceleration thresholds, signal strength weighting)
- #79: +10.2255 (enhanced momentum + volatility-adjusted percentile)
- #78: +9.9073 (dual-regime confirmation, discrete asymmetric scaling)

Key observation: The best performers (#79, #80) all score around 10.2,
suggesting a plateau. Trial #81 dropped to 7.78, indicating the regime-adaptive
confirmation bars may be too aggressive in changing entry frequency.

New hypothesis for improvement:
- #80 peaked at 10.2255 with fixed confirmation_bars=2 and signal_strength >= 1.5
- #81 dropped by using regime-adaptive confirmation bars (1-3 range)
- Problem: making confirmation bars regime-adaptive reduced entry consistency
- Observation: the fixed 1.5x signal strength threshold was more stable
- New approach: REVERT TO STABLE CORE WITH REFINED DELTA FILTERING
- Keep the proven fixed confirmation_bars=2 from #80
- Keep the proven signal_strength >= 1.5 threshold from #80
- But enhance the delta signal detection itself:
  - Current: uses percentile-based absolute delta threshold
  - Problem: percentile doesn't capture recent delta regime changes well
  - New: use adaptive percentile that weights recent bars more heavily
  - Weight recent deltas (last 4 bars) 2x, older deltas 1x in percentile calc
  - This makes threshold more responsive to current market regime
- Additionally, refine the momentum check:
  - Current: requires current_delta > adaptive_threshold * prev_delta
  - Problem: single-bar momentum can be noisy
  - New: use a smoother momentum check
  - Compute 2-bar delta momentum: (current + prev) / 2 vs threshold
  - This smooths out single-bar noise
- Combine with proven components for synergy

This strategy refines #80 by:
1. Keeping proven core from #80:
   - Fixed confirmation_bars=2 (not regime-adaptive)
   - Signal strength >= 1.5 threshold (not regime-adaptive)
   - Adaptive acceleration thresholds per volatility regime
   - Volatility-adjusted delta percentile [75%, 70%, 65%]
   - Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85]
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Combined volatility ratio scaling [0.6, 1.8]
   - Smooth momentum adaptation [0.6, 0.5, 0.4]

2. Adding weighted percentile calculation:
   - Recent deltas (last 4 bars) weighted 2x
   - Older deltas weighted 1x
   - Makes threshold more responsive to current regime
   - Captures recent delta behavior better

3. Adding smoothed momentum check:
   - Use 2-bar delta momentum: (current + prev) / 2
   - Reduces single-bar noise in momentum detection
   - More robust to isolated spikes

4. Maintaining all proven components:
   - Discrete asymmetric bracket scaling (proven at #68, #78, #79, #80)
   - Adaptive acceleration thresholds (proven at #80)
   - Dual-regime entry confirmation (proven at #78, #79, #80)
   - Multi-bar confirmation with price direction
   - Momentum confirmation with acceleration

Key hypothesis:
- #80 is strong at 10.2255 with fixed confirmation bars and signal strength
- #81 dropped by making confirmation bars regime-adaptive
- Reverting to fixed confirmation bars but with refined delta filtering should improve
- Weighted percentile makes threshold more responsive without changing entry frequency
- Smoothed momentum check reduces false signals
- Should match or exceed #80 by stabilizing entry logic while improving signal quality
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

    def _compute_weighted_percentile_threshold(self, deltas_list: list[float], percentile: float) -> float:
        """
        Compute percentile threshold with recent deltas weighted more heavily.
        
        Recent deltas (last 4 bars) are weighted 2x, older deltas weighted 1x.
        This makes the threshold more responsive to current market regime.
        """
        if len(deltas_list) < 2:
            return 0.5
        
        abs_deltas = [abs(d) for d in deltas_list]
        
        # Create weighted list: recent bars (last 4) weighted 2x
        weighted_deltas = []
        for i, d in enumerate(abs_deltas):
            if i >= len(abs_deltas) - 4:
                # Recent bars: weight 2x
                weighted_deltas.extend([d, d])
            else:
                # Older bars: weight 1x
                weighted_deltas.append(d)
        
        weighted_deltas.sort()
        
        if len(weighted_deltas) == 0:
            return 0.5
        
        idx = int(percentile * (len(weighted_deltas) - 1))
        idx = max(0, min(idx, len(weighted_deltas) - 1))
        
        return weighted_deltas[idx]

    def _check_smoothed_momentum(
        self, current_delta: float, signal: int, adaptive_threshold: float, vol_ratio: float
    ) -> bool:
        """
        Check if delta momentum aligns with signal direction using smoothed 2-bar momentum.
        
        Uses 2-bar average momentum to reduce single-bar noise.
        """
        if signal == 0:
            return False
        
        # Use 2-bar momentum: average of current and previous
        avg_delta = (current_delta + self.prev_delta) / 2.0
        
        # Check base momentum with smoothed average
        if signal > 0:
            momentum_ok = avg_delta > adaptive_threshold * self.prev_delta
        else:
            momentum_ok = avg_delta < adaptive_threshold * self.prev_delta
        
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
        
        # Get volatility-adjusted percentile
        delta_percentile = self._get_volatility_adjusted_percentile(vol_ratio)
        
        # Compute delta threshold using weighted percentile
        delta_threshold = self._compute_weighted_percentile_threshold(deltas_list, delta_percentile)
        
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
            
            # Check smoothed momentum confirmation
            momentum_ok = self._check_smoothed_momentum(
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
            
            # Determine confirmation requirements (fixed from #80, not regime-adaptive)
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
