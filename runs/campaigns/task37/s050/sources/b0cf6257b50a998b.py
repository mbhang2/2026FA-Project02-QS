"""
An order flow strategy with refined signal quality through selective entry confirmation.

Key insights from prior trials:

1. Trial #60 (score +5.8968) - BEST:
   - Smooth volatility-adaptive momentum threshold
   - Linear interpolation between regimes prevents whipsaws
   - 70th percentile delta threshold is robust
   - Multi-bar confirmation with strict price direction
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #61 (score +5.8968) - IDENTICAL TO #60:
   - Adaptive percentile based on delta volatility
   - No improvement from added complexity

3. Core pattern analysis:
   - #60/#61 at 5.8968 is the peak
   - The 70th percentile delta threshold is robust
   - Multi-bar confirmation with price direction is critical
   - Smooth transitions beat sharp thresholds
   - Simplicity and consistency beat added complexity

4. New hypothesis for improvement over #60:
   - #60 is strong at 5.8968 - smooth momentum adaptation works
   - Opportunity: SELECTIVE ENTRY CONFIRMATION
   - Current: requires ALL conditions (AND logic):
     - price above/below MA
     - signal alignment (majority rule)
     - current signal present
     - price direction confirmation
     - momentum confirmation
   - Issue: too many conditions create dead zones where valid moves are missed
   - New approach: WEIGHTED SCORING instead of AND logic
   - Assign points for each condition met
   - Enter when score exceeds threshold (e.g., 4 of 5 conditions)
   - This captures valid entries that miss ONE condition
   - Should improve capture rate without increasing false signals

5. Weighted entry confirmation approach:
   - Score each confirmation condition: 0 or 1 point
   - Condition 1: price above/below MA (1 point)
   - Condition 2: signal alignment - majority rule (1 point)
   - Condition 3: current signal present (1 point)
   - Condition 4: recent price direction alignment (1 point)
   - Condition 5: adaptive momentum confirmation (1 point)
   - Enter when score >= 4 (at least 4 of 5 conditions)
   - This is more flexible than requiring all 5
   - Captures moves that miss one condition
   - Still maintains quality through majority voting

6. Refinement to scoring:
   - Keep all parameters from #60 exactly
   - Replace AND logic with weighted score >= 4
   - This allows one condition to fail while still entering
   - Hypothesis: real moves often have one weak signal
   - Current strict AND logic may be TOO conservative
   - Score >= 4 maintains quality while improving capture

This strategy refines #60 by:
1. Keeping all core logic from #60 exactly:
   - Smooth volatility-adaptive momentum threshold
   - Combined volatility ratio scaling [0.6, 1.8]
   - MA period 10, delta_lookback 14, vol_lookback 12
   - All parameter values and thresholds unchanged

2. Replacing AND logic with weighted scoring:
   - Each confirmation condition worth 1 point
   - Enter when score >= 4 (at least 4 of 5)
   - More flexible than requiring all conditions
   - Still maintains signal quality through majority voting
   - Captures valid entries that miss one condition

3. Maintaining all proven parameters:
   - MA period 10, delta lookback 14, vol lookback 12
   - Base stop/target offset 0.75
   - Combined vol ratio [0.6, 1.8]
   - Smooth momentum adaptation [0.6, 0.5, 0.4]
   - 70th percentile delta threshold

Key hypothesis:
- #60 is strong at 5.8968 with smooth momentum adaptation
- Weighted scoring improves entry capture vs strict AND logic
- Real moves often fail one confirmation condition
- Score >= 4 maintains quality while being less restrictive
- Should improve trade count and capture rate
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
            # Linear interpolation from 0.6 to 0.5 over [0.7, 0.9]
            t = (vol_ratio - 0.7) / (0.9 - 0.7)
            return 0.6 - t * 0.1
        elif vol_ratio < 1.1:
            return 0.5
        elif vol_ratio < 1.3:
            # Linear interpolation from 0.5 to 0.4 over [1.1, 1.3]
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
            # Buy signal: require current_delta > threshold * previous_delta
            return current_delta > adaptive_threshold * self.prev_delta
        else:
            # Sell signal: require current_delta < threshold * previous_delta
            return current_delta < adaptive_threshold * self.prev_delta

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
        
        # Compute delta threshold from percentile of absolute deltas
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # 70th percentile for threshold (proven in #60)
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
        
        # Compute volatility adjustment using 65th percentile (like #60)
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
        
        stop_offset = self.base_stop_offset * combined_vol_ratio
        target_offset = self.base_target_offset * combined_vol_ratio
        
        # Weighted confirmation scoring: enter when score >= 4 of 5
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
            
            # Weighted scoring for LONG entry
            if signal > 0:
                long_score = 0
                
                # Condition 1: price above MA (1 point)
                if price_above_ma:
                    long_score += 1
                
                # Condition 2: signal alignment - majority rule (1 point)
                if positive_signals >= self.confirmation_bars - 1:
                    long_score += 1
                
                # Condition 3: current signal positive (1 point)
                if signal > 0:
                    long_score += 1
                
                # Condition 4: recent price direction alignment (1 point)
                if positive_price_dirs >= len(price_dirs_list) - 1:
                    long_score += 1
                
                # Condition 5: adaptive momentum confirmation (1 point)
                if momentum_ok:
                    long_score += 1
                
                # Enter if score >= 4 (at least 4 of 5 conditions met)
                if long_score >= 4:
                    ctx.buy(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="entry",
                    )
            
            # Weighted scoring for SHORT entry
            elif signal < 0:
                short_score = 0
                
                # Condition 1: price below MA (1 point)
                if price_below_ma:
                    short_score += 1
                
                # Condition 2: signal alignment - majority rule (1 point)
                if negative_signals >= self.confirmation_bars - 1:
                    short_score += 1
                
                # Condition 3: current signal negative (1 point)
                if signal < 0:
                    short_score += 1
                
                # Condition 4: recent price direction alignment (1 point)
                if negative_price_dirs >= len(price_dirs_list) - 1:
                    short_score += 1
                
                # Condition 5: adaptive momentum confirmation (1 point)
                if momentum_ok:
                    short_score += 1
                
                # Enter if score >= 4 (at least 4 of 5 conditions met)
                if short_score >= 4:
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
