"""
An order flow strategy with refined delta acceleration and dynamic confirmation.

Key insights from trial #72 (score +8.3328):
- Delta acceleration confirmation improves entry timing in low vol
- Asymmetric bracket scaling based on volatility trend is effective
- Smooth momentum adaptation [0.6, 0.5, 0.4] across regimes
- 70th percentile delta threshold is proven optimal
- Fixed 1.5x signal strength threshold works best
- Multi-bar confirmation with price direction alignment

Analysis of recent trials:
- #68: +8.3800 (baseline with asymmetric scaling)
- #71: +7.7419 (regime-adaptive threshold was too strict)
- #72: +8.3328 (delta acceleration helps but slightly lower than #68)

Key observation: #68 at 8.3800 remains the peak. The core logic is sound.
The issue: subtle variations in confirmation logic produce marginal changes.

New hypothesis for improvement over #72:
- #68 is the strongest at 8.3800 with proven entry logic
- Current issue: confirmation counting treats all 5 conditions equally
- Observation: not all conditions are equally predictive
- Problem: weighting all conditions the same may dilute signal quality
- New approach: WEIGHTED CONFIRMATION WITH PRIORITY CONDITIONS
- Some conditions are stronger predictors than others:
  1. Delta signal magnitude (absolute, not just direction) - strongest
  2. Price direction confirmation (momentum alignment) - strong
  3. Multi-bar trend signal (persistence) - strong
  4. Moving average alignment (trend filter) - moderate
  5. Momentum check (adaptive threshold) - moderate
- Weight conditions by predictive strength
- Require strong conditions + weaker conditions to add up
- Delta signal + price direction alone should NOT be enough
- But delta signal + price direction + one other = entry
- This refines confirmation by using condition quality, not just count

This strategy refines #68 by:
1. Keeping all core logic from #68 exactly:
   - Smooth volatility-adaptive momentum threshold [0.6, 0.5, 0.4]
   - 70th percentile delta threshold (fixed)
   - Multi-bar confirmation with price direction
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Combined volatility ratio scaling [0.6, 1.8]
   - Asymmetric bracket scaling based on volatility trend

2. Adding weighted confirmation logic:
   - Delta signal strength (how far beyond threshold): weight 2.0
   - Price direction alignment: weight 1.5
   - Multi-bar trend signal: weight 1.5
   - Moving average alignment: weight 1.0
   - Momentum check: weight 1.0
   - Total weight needed: 4.5 for strong signal, 5.5 for weak signal
   - This ensures delta signal is a necessary component
   - Prevents entries on weak delta signals even with other alignment
   - Should improve entry quality by prioritizing strongest signals

3. Maintaining all proven components:
   - Fixed 1.5x signal strength threshold
   - Smooth momentum adaptation [0.6, 0.5, 0.4]
   - 70th percentile delta threshold
   - Price direction confirmation
   - Asymmetric bracket scaling

Key hypothesis:
- #68 is strong at 8.3800 with proven entry logic
- Weighted confirmation improves entry quality
- Delta signal strength should be a necessary condition
- Combining strong predictors is better than counting all equally
- This adapts confirmation by signal quality, not just presence
- Should improve robustness by prioritizing strongest signals
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
        Compute asymmetric bracket scaling based on volatility trend.
        
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
        
        # Asymmetric scaling based on volatility trend
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
        
        # Get asymmetric bracket scaling based on volatility trend
        stop_scale, target_scale = self._get_asymmetric_bracket_scaling(
            list(self.ranges), list(self.longer_ranges)
        )
        
        stop_offset = self.base_stop_offset * combined_vol_ratio * stop_scale
        target_offset = self.base_target_offset * combined_vol_ratio * target_scale
        
        # Weighted confirmation logic
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Check adaptive momentum confirmation
            momentum_ok = self._check_momentum(current_delta, signal, adaptive_momentum)
            
            # Compute signal strength: how far beyond threshold is this signal?
            if adapted_threshold > 0:
                signal_strength = abs(current_delta) / adapted_threshold
            else:
                signal_strength = 1.0
            
            # Determine if signal is strong (>= 1.5x threshold)
            is_strong_signal = signal_strength >= 1.5
            
            # Long entry with weighted confirmation
            if signal > 0:
                weight = 0.0
                
                # Delta signal strength: weight 2.0 (most important)
                if signal > 0:
                    weight += signal_strength * 2.0
                
                # Price direction alignment: weight 1.5
                if positive_price_dirs >= len(price_dirs_list) - 1:
                    weight += 1.5
                
                # Multi-bar trend signal: weight 1.5
                if positive_signals >= self.confirmation_bars - 1:
                    weight += 1.5
                
                # Moving average alignment: weight 1.0
                if price_above_ma:
                    weight += 1.0
                
                # Momentum check: weight 1.0
                if momentum_ok:
                    weight += 1.0
                
                # Entry thresholds: strong signal needs 4.5, weak needs 5.5
                min_weight = 4.5 if is_strong_signal else 5.5
                
                if weight >= min_weight:
                    ctx.buy(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="entry",
                    )
            
            # Short entry with weighted confirmation
            elif signal < 0:
                weight = 0.0
                
                # Delta signal strength: weight 2.0 (most important)
                if signal < 0:
                    weight += signal_strength * 2.0
                
                # Price direction alignment: weight 1.5
                if negative_price_dirs >= len(price_dirs_list) - 1:
                    weight += 1.5
                
                # Multi-bar trend signal: weight 1.5
                if negative_signals >= self.confirmation_bars - 1:
                    weight += 1.5
                
                # Moving average alignment: weight 1.0
                if price_below_ma:
                    weight += 1.0
                
                # Momentum check: weight 1.0
                if momentum_ok:
                    weight += 1.0
                
                # Entry thresholds: strong signal needs 4.5, weak needs 5.5
                min_weight = 4.5 if is_strong_signal else 5.5
                
                if weight >= min_weight:
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
