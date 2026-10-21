"""
An order flow strategy with refined regime detection and enhanced entry quality.

Key insights from trial #80 (score +10.2255, current best):
- Enhanced momentum confirmation with adaptive acceleration thresholds is effective
- Volatility-adjusted delta percentile improves regime-aware filtering
- Discrete asymmetric bracket scaling remains optimal
- Multi-bar confirmation with price direction alignment is robust
- Smooth momentum adaptation [0.6, 0.5, 0.4] works well

Analysis of peak performers:
- #80: +10.2255 (adaptive acceleration thresholds, signal strength weighting)
- #79: +10.2255 (enhanced momentum + volatility-adjusted percentile)
- #78: +9.9073 (dual-regime confirmation, discrete asymmetric scaling)

Key observation: #80 achieved +10.2255 by weighting signal strength by
both magnitude and acceleration. The approach of adaptive acceleration
thresholds per volatility regime is effective.

New hypothesis for improvement over #80:
- #80 is strong at 10.2255 with proven adaptive acceleration thresholds
- Current issue: signal strength calculation uses fixed 0.5 cap on accel_weight
- Observation: this caps the benefit of strong momentum builds
- Problem: high-quality momentum builds (strong accel) don't get full credit
- New approach: MOMENTUM-AWARE SIGNAL STRENGTH WITH REGIME SCALING
- Instead of fixed 0.5 cap, scale the acceleration weight by vol_ratio:
  - Low vol (vol_ratio < 0.8): cap 0.3 (conservative, less weight to accel)
  - Normal vol (0.8 <= vol_ratio <= 1.2): cap 0.5 (baseline)
  - High vol (vol_ratio > 1.2): cap 0.8 (aggressive, more weight to accel)
- This gives stronger entries in high vol where momentum is more valuable
- Additionally, refine the confirmation bar logic:
  - Current: confirmation_bars = 2 (hardcoded)
  - Problem: fixed window doesn't adapt to market regime
  - New: make confirmation_bars regime-adaptive
  - Low vol: require 3-bar confirmation (stricter)
  - Normal vol: require 2-bar confirmation (baseline)
  - High vol: require 1-bar confirmation (looser)
- This makes the multi-bar confirmation regime-aware
- Combine with existing signal strength logic for synergy

This strategy refines #80 by:
1. Keeping core logic from #80:
   - Adaptive acceleration thresholds per volatility regime
   - Volatility-adjusted delta percentile [75%, 70%, 65%]
   - Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85]
   - Dual-regime entry confirmation (strict/balanced/relaxed)
   - Multi-bar confirmation with price direction
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Combined volatility ratio scaling [0.6, 1.8]
   - Smooth momentum adaptation [0.6, 0.5, 0.4]

2. Adding momentum-aware signal strength scaling:
   - Scale acceleration weight cap by volatility regime
   - Low vol: cap 0.3 (conservative)
   - Normal vol: cap 0.5 (baseline)
   - High vol: cap 0.8 (aggressive)
   - Gives stronger entries in high vol where momentum matters more
   - Filters weak entries in low vol where noise is higher

3. Making confirmation bars regime-adaptive:
   - Low vol (vol_ratio < 0.8): 3-bar confirmation
   - Normal vol (0.8 <= vol_ratio <= 1.2): 2-bar confirmation
   - High vol (vol_ratio > 1.2): 1-bar confirmation
   - Adapts multi-bar confirmation to volatility regime
   - Stricter in low vol, looser in high vol

4. Maintaining all proven components:
   - Discrete asymmetric bracket scaling (proven at #68, #78, #79, #80)
   - Adaptive acceleration thresholds (proven at #80)
   - Volatility-adjusted delta percentile (proven at #79, #80)
   - Fixed 1.5x signal strength threshold
   - Price direction confirmation
   - Momentum confirmation with acceleration

Key hypothesis:
- #80 is strong at 10.2255 with proven adaptive acceleration thresholds
- Momentum-aware signal strength scaling improves entry quality in different regimes
- Regime-adaptive confirmation bars capture more high-quality entries in high vol
- Combined approach should improve or match #80 by being more regime-sensitive
- Should capture stronger momentum builds while filtering noise effectively
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
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
        # Track closes for moving average
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track deltas for distribution analysis
        self.deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track bar ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track trend signals for multi-bar confirmation (max 3 for adaptive)
        self.trend_signals: deque[int] = deque(maxlen=3)
        
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

    def _get_regime_adaptive_confirmation_bars(self, vol_ratio: float) -> int:
        """
        Get confirmation bar requirement based on volatility regime.
        
        Low vol (vol_ratio < 0.8): 3-bar confirmation (stricter)
        Normal vol (0.8 <= vol_ratio <= 1.2): 2-bar confirmation (baseline)
        High vol (vol_ratio > 1.2): 1-bar confirmation (looser)
        """
        if vol_ratio < 0.8:
            return 3
        elif vol_ratio > 1.2:
            return 1
        else:
            return 2

    def _get_momentum_aware_accel_weight_cap(self, vol_ratio: float) -> float:
        """
        Get acceleration weight cap based on volatility regime.
        
        Low vol (vol_ratio < 0.8): 0.3 (conservative)
        Normal vol (0.8 <= vol_ratio <= 1.2): 0.5 (baseline)
        High vol (vol_ratio > 1.2): 0.8 (aggressive)
        """
        if vol_ratio < 0.8:
            return 0.3
        elif vol_ratio > 1.2:
            return 0.8
        else:
            return 0.5

    def _check_momentum_with_adaptive_acceleration(
        self, current_delta: float, signal: int, adaptive_threshold: float, vol_ratio: float
    ) -> bool:
        """Check if delta momentum aligns with signal direction AND delta is building."""
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
        
        # Get regime-adaptive confirmation bars
        confirmation_bars = self._get_regime_adaptive_confirmation_bars(vol_ratio)
        
        # Get momentum-aware acceleration weight cap
        accel_weight_cap = self._get_momentum_aware_accel_weight_cap(vol_ratio)
        
        # Signal strength-based confirmation
        if len(self.trend_signals) >= confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Check momentum confirmation
            momentum_ok = self._check_momentum_with_adaptive_acceleration(
                current_delta, signal, regime_momentum, vol_ratio
            )
            
            # Compute delta acceleration
            delta_accel = current_delta - self.prev_delta
            
            # Compute signal strength with momentum-aware acceleration weighting
            if adapted_threshold > 0:
                signal_strength = abs(current_delta) / adapted_threshold
                # Weight by acceleration with regime-adaptive cap
                accel_weight = 1.0 + min(abs(delta_accel) / adapted_threshold, accel_weight_cap)
                signal_strength *= accel_weight
            else:
                signal_strength = 1.0
            
            # Determine confirmation requirements
            if regime_min_conditions == -1:
                # Normal volatility: use signal strength logic
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
                if positive_signals >= confirmation_bars - 1:
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
                if negative_signals >= confirmation_bars - 1:
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
