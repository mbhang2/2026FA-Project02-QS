"""
An order flow strategy with dual-regime entry confirmation and dynamic bracket optimization.

Key insights from trial #77 (score +6.2955, recent best):
- Gradient-based asymmetric bracket scaling provides smooth regime transitions
- Smooth volatility-adaptive momentum threshold [0.6, 0.5, 0.4] is robust
- 70th percentile delta threshold remains optimal
- Fixed 1.5x signal strength threshold (strong: 4 of 5, weak: all 5) is proven
- Multi-bar confirmation with price direction alignment works well

Analysis of peak performers:
- #68: +8.3800 (peak with asymmetric scaling, discrete regimes)
- #77: +6.2955 (gradient-based scaling, smooth transitions)
- #72: +8.3328 (with delta acceleration)
- #64: +6.0957 (original asymmetric approach)

Key observation: #68 achieved +8.3800 with discrete asymmetric scaling
but #77 with gradient scaling dropped to +6.2955. The discrete thresholds
(vol_trend > 1.1 vs < 0.9) appear more effective than smooth gradients.

New hypothesis for improvement over #68:
- #68 is strong at 8.3800 with proven discrete asymmetric scaling
- Current issue: entry confirmation uses single momentum check
- Observation: momentum check compares to prev_delta which can be noisy
- Problem: single-bar comparison may miss sustained momentum builds
- New approach: DUAL-REGIME ENTRY CONFIRMATION
- Regime 1 (low volatility, vol_ratio < 0.8): Strict confirmation
  - Require ALL 5 conditions (no signal strength relaxation)
  - Momentum must exceed 0.6x threshold (strict)
  - This catches only the cleanest entries in choppy markets
- Regime 2 (normal volatility, 0.8 <= vol_ratio <= 1.2): Balanced
  - Use proven signal strength logic (4 of 5 for strong, all 5 for weak)
  - Momentum threshold 0.5x (normal)
  - This is the baseline proven approach
- Regime 3 (high volatility, vol_ratio > 1.2): Relaxed confirmation
  - Require only 3 of 5 conditions (more opportunities in volatility)
  - Momentum threshold 0.4x (loose)
  - This captures more entries when vol is elevated
- This makes entry filtering regime-aware, not just bracket scaling
- Should improve robustness by adapting confirmation to market conditions

This strategy refines #68 by:
1. Keeping core logic from #68:
   - Discrete asymmetric bracket scaling [0.85/1.15 vs 1.15/0.85]
   - 70th percentile delta threshold (fixed)
   - Multi-bar confirmation with price direction
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Combined volatility ratio scaling [0.6, 1.8]

2. Adding dual-regime entry confirmation:
   - Low vol (< 0.8): ALL 5 conditions, strict 0.6x momentum
   - Normal vol (0.8-1.2): Signal strength logic, 0.5x momentum
   - High vol (> 1.2): Only 3 of 5, loose 0.4x momentum
   - Adapts confirmation strictness to volatility regime
   - No new parameters, just regime-aware confirmation

3. Maintaining all proven components:
   - Discrete asymmetric bracket scaling (proven at #68)
   - Fixed 1.5x signal strength threshold
   - 70th percentile delta threshold
   - Price direction confirmation
   - Multi-bar confirmation with majority rule

Key hypothesis:
- #68 is strong at 8.3800 with proven discrete asymmetric scaling
- Dual-regime entry confirmation improves entry quality
- Low volatility needs stricter confirmation to avoid false entries
- High volatility allows more entries to capture momentum
- Normal volatility uses proven baseline approach
- Should improve robustness without changing bracket logic
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

    def _check_momentum(self, current_delta: float, signal: int, adaptive_threshold: float) -> bool:
        """
        Check if delta momentum aligns with signal direction.
        Uses adaptive threshold based on volatility regime.
        """
        if signal == 0:
            return False
        
        if signal > 0:
            return current_delta > adaptive_threshold * self.prev_delta
        else:
            return current_delta < adaptive_threshold * self.prev_delta

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
        
        # Discrete asymmetric scaling based on volatility trend (proven at #68)
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
        
        # Compute delta threshold from percentile of absolute deltas
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # 70th percentile for threshold (proven in #60, #64, #68)
        threshold_idx = int(0.70 * (len(abs_deltas_sorted) - 1))
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
        
        # Get smooth adaptive momentum threshold based on volatility regime
        adaptive_momentum = self._get_smooth_adaptive_momentum(vol_ratio)
        
        # Scale bracket levels based on both price volatility and delta volatility
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.6, min(combined_vol_ratio, 1.8))
        
        # Get asymmetric bracket scaling (discrete regimes from #68)
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
            
            # Check adaptive momentum confirmation
            momentum_ok = self._check_momentum(current_delta, signal, regime_momentum)
            
            # Compute signal strength: how far beyond threshold is this signal?
            if adapted_threshold > 0:
                signal_strength = abs(current_delta) / adapted_threshold
            else:
                signal_strength = 1.0
            
            # Determine confirmation requirements based on regime
            if regime_min_conditions == -1:
                # Normal volatility: use signal strength logic (proven from #68)
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
