"""
An order flow strategy with enhanced signal quality through adaptive threshold scaling.

Key insights from prior trials:

1. Trial #64 (score +6.0957) - BEST:
   - Signal strength-based confirmation (4 of 5 for strong, all 5 for weak)
   - Smooth volatility-adaptive momentum threshold
   - 70th percentile delta threshold
   - Multi-bar confirmation with price direction
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Combined volatility ratio scaling [0.6, 1.8]

2. Trial #60 (score +5.8968) - BASELINE:
   - Strict AND logic (all 5 conditions required)
   - Same core parameters as #64
   - Signal strength adaptation was the key improvement

3. Core pattern analysis:
   - #64 at 6.0957 is the peak with signal strength-based confirmation
   - Strong signals (>= 1.5x threshold) need only 4 of 5 conditions
   - Weak signals (< 1.5x threshold) need all 5 conditions
   - This adaptive logic improved over strict AND by ~0.2 points
   - The 1.5x threshold for "strong signal" is the critical parameter

4. New hypothesis for improvement over #64:
   - #64 is strong at 6.0957 - signal strength adaptation works
   - Current: fixed 1.5x multiplier for strong signal detection
   - Issue: 1.5x may not be optimal for all market regimes
   - New approach: ADAPTIVE SIGNAL STRENGTH THRESHOLD
   - In calm markets (low volatility): require higher multiplier (1.6-1.7x)
     - Fewer strong signals, but higher quality
   - In normal markets: use baseline (1.5x)
   - In volatile markets: use lower multiplier (1.3-1.4x)
     - More signals captured, but with 4-of-5 safety check
   - This makes signal strength detection regime-aware
   - Should improve entry quality in calm markets
   - Should capture more opportunities in volatile markets

5. Volatility-adaptive signal strength approach:
   - Compute combined volatility ratio (price_vol * delta_vol)
   - If combined_vol < 0.8 (calm): signal_strength_threshold = 1.7
   - If combined_vol 0.8-1.2 (normal): signal_strength_threshold = 1.5
   - If combined_vol > 1.2 (volatile): signal_strength_threshold = 1.3
   - Use smooth interpolation at boundaries
   - This adapts confirmation strictness to market regime

This strategy refines #64 by:
1. Keeping all core logic from #64 exactly:
   - Signal strength-based confirmation (4 of 5 vs all 5)
   - Smooth volatility-adaptive momentum threshold
   - 70th percentile delta threshold
   - Multi-bar confirmation with price direction
   - MA period 10, delta_lookback 14, vol_lookback 12
   - All parameter values unchanged

2. Adding volatility-adaptive signal strength threshold:
   - Compute combined volatility regime
   - Calm markets (< 0.8): threshold = 1.7 (stricter, fewer signals)
   - Normal markets (0.8-1.2): threshold = 1.5 (baseline)
   - Volatile markets (> 1.2): threshold = 1.3 (looser, more signals)
   - Use smooth interpolation at regime boundaries
   - This makes strong/weak signal detection regime-aware

3. Maintaining all proven parameters:
   - MA period 10, delta lookback 14, vol lookback 12
   - Base stop/target offset 0.75
   - Smooth momentum adaptation [0.6, 0.5, 0.4]
   - 70th percentile delta threshold
   - Multi-bar confirmation with majority rule

Key hypothesis:
- #64 is strong at 6.0957 with signal strength-based confirmation
- Volatility-adaptive signal strength threshold improves entry quality
- Calm markets benefit from stricter signal strength requirements
- Volatile markets benefit from looser signal strength requirements
- This adapts confirmation to market regime while keeping the proven 4-of-5 logic
- Should improve robustness across different market conditions
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
            t = (vol_ratio - 0.7) / (0.9 - 0.7)
            return 0.6 - t * 0.1
        elif vol_ratio < 1.1:
            return 0.5
        elif vol_ratio < 1.3:
            t = (vol_ratio - 1.1) / (1.3 - 1.1)
            return 0.5 - t * 0.1
        else:
            return 0.4

    def _get_adaptive_signal_strength_threshold(self, combined_vol: float) -> float:
        """
        Get signal strength threshold adapted to market regime.
        
        combined_vol < 0.8: 1.7 (calm regime, stricter)
        combined_vol 0.8-1.2: interpolate 1.7 → 1.5 → 1.3
        combined_vol > 1.2: 1.3 (volatile regime, looser)
        """
        if combined_vol < 0.8:
            return 1.7
        elif combined_vol < 1.2:
            # Linear interpolation from 1.7 to 1.3 over [0.8, 1.2]
            t = (combined_vol - 0.8) / (1.2 - 0.8)
            return 1.7 - t * 0.4
        else:
            return 1.3

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
        
        stop_offset = self.base_stop_offset * combined_vol_ratio
        target_offset = self.base_target_offset * combined_vol_ratio
        
        # Get volatility-adaptive signal strength threshold
        signal_strength_threshold = self._get_adaptive_signal_strength_threshold(combined_vol_ratio)
        
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
            momentum_ok = self._check_momentum(current_delta, signal, adaptive_momentum)
            
            # Compute signal strength: how far beyond threshold is this signal?
            if adapted_threshold > 0:
                signal_strength = abs(current_delta) / adapted_threshold
            else:
                signal_strength = 1.0
            
            # Strong signal (>= threshold): 4 of 5 conditions needed
            # Weak signal (< threshold): all 5 conditions needed
            is_strong_signal = signal_strength >= signal_strength_threshold
            
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
                
                # Strong signal: need 4 of 5, Weak signal: need all 5
                min_conditions = 4 if is_strong_signal else 5
                
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
                
                # Strong signal: need 4 of 5, Weak signal: need all 5
                min_conditions = 4 if is_strong_signal else 5
                
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
