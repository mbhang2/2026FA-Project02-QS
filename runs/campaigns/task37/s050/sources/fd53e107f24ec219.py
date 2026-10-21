"""
An order flow strategy with refined signal strength filtering and adaptive confirmation.

Key insights from prior trials:

1. Trial #36 (score +5.8422) - BEST OVERALL:
   - Soft delta momentum confirmation with 0.5x threshold
   - Adaptive delta threshold from 70th percentile
   - Combined volatility ratio scaling [0.6, 1.8]
   - Multi-bar confirmation with price direction alignment
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #55 (score +4.5439):
   - Added extremeness ranking (top/bottom 30% of recent deltas)
   - Double confirmation (momentum + extremeness) was too restrictive
   - Filtered out too many valid entries
   - Score dropped from #36's 5.8422 to 4.5439

3. Trial #54 (score +3.4626):
   - Mean reversion confirmation was even more restrictive
   - Trying to capture turning points missed continuation trades
   - Score dropped significantly

4. Trial #51 (score +5.8128):
   - Signal strength-weighted momentum (0.2x for strong, 0.35x for normal, 0.5x for weak)
   - Very close to #36 at 5.8128 vs 5.8422
   - Only 0.0294 behind - adaptive momentum based on signal strength was nearly optimal

5. Core pattern analysis:
   - #36 at 5.8422 is the peak with 0.5x FIXED momentum
   - #51 at 5.8128 with ADAPTIVE momentum (0.2-0.5x) was close but slightly worse
   - #55 at 4.5439 shows that adding extremeness filtering over-constrains
   - The problem: #51 tried to be smarter but simplicity wins
   - The opportunity: adaptive momentum based on SIGNAL STRENGTH might work differently

6. New hypothesis for improvement over #36:
   - #36 is strong at 5.8422 with fixed 0.5x momentum
   - #51 tried adaptive momentum (0.2x for strong signals) but was slightly worse
   - Problem with #51: inverse relationship (stronger signal = lower momentum threshold)
     made strong signals harder to confirm
   - New approach: RELAXED momentum for STRONG signals, STRICT for WEAK signals
   - For strong buy signals (delta >> threshold): require current > 0.3 * previous
   - For normal buy signals (delta > threshold): require current > 0.5 * previous
   - For strong sell signals: require current < 0.3 * previous
   - For normal sell signals: require current < 0.5 * previous
   - This captures strong order flow extremes with relaxed momentum
   - Weak signals get strict momentum confirmation
   - Intuition: very extreme delta should be easier to confirm (it's obvious)
   - Marginal delta needs stronger momentum to avoid noise

7. Signal strength ranking approach:
   - Measure how extreme the signal is relative to recent distribution
   - If current_delta is in top 10% of recent deltas -> strong signal
   - If current_delta is in top 30% of recent deltas -> normal signal
   - Use 0.3x momentum for strong, 0.5x for normal
   - This is the OPPOSITE of #51's approach
   - Strong extremes should be easier to trade, not harder

This strategy refines #36 by:
1. Keeping all core logic from #36 exactly:
   - 70th percentile delta threshold for signal detection
   - Adaptive delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8]
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Multi-bar confirmation with majority rule
   - Price direction confirmation

2. Replacing fixed 0.5x momentum with signal-strength-aware momentum:
   - Classify signal as STRONG (top 10%) or NORMAL (top 30%)
   - For strong signals: require 0.3x previous delta momentum
   - For normal signals: require 0.5x previous delta momentum
   - This relaxes confirmation for extreme order flow
   - Tightens confirmation for marginal signals
   - Inverse of #51: stronger signals get EASIER confirmation

3. Maintaining all proven parameters:
   - MA period 10, delta lookback 14, vol lookback 12
   - Confirmation bars 2 with majority rule
   - Base stop/target offset 0.75
   - Combined vol ratio [0.6, 1.8]
   - 70th percentile delta threshold

Key hypothesis:
- #36 is strong at 5.8422 with fixed 0.5x momentum
- #51 tried adaptive momentum but got it backwards (harder for strong signals)
- New approach: easier confirmation for EXTREME deltas, harder for MARGINAL ones
- Should improve on #36 by trading strong order flow extremes more aggressively
- More intuitive than #51: extreme signals should be easier to act on
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
        strong_momentum_threshold: float = 0.3,
        normal_momentum_threshold: float = 0.5,
        strong_signal_percentile: float = 0.9,
        normal_signal_percentile: float = 0.7,
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
            strong_momentum_threshold: momentum multiplier for strong signals (0.3)
            normal_momentum_threshold: momentum multiplier for normal signals (0.5)
            strong_signal_percentile: percentile threshold for strong signals (0.9 = top 10%)
            normal_signal_percentile: percentile threshold for normal signals (0.7 = top 30%)
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.confirmation_bars = confirmation_bars
        self.strong_momentum_threshold = strong_momentum_threshold
        self.normal_momentum_threshold = normal_momentum_threshold
        self.strong_signal_percentile = strong_signal_percentile
        self.normal_signal_percentile = normal_signal_percentile
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
        # Track closes for moving average
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track deltas for distribution analysis
        self.deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track bar ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track recent deltas for signal strength ranking (10-bar window)
        self.recent_deltas: deque[float] = deque(maxlen=10)
        
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

    def _classify_signal_strength(self, current_delta: float, signal: int) -> tuple[int, float]:
        """
        Classify signal as STRONG (top 10%), NORMAL (top 30%), or WEAK.
        Returns (strength_level, momentum_threshold) where:
        - 2 = strong (use 0.3x momentum)
        - 1 = normal (use 0.5x momentum)
        - 0 = weak (don't trade)
        """
        if signal == 0 or len(self.recent_deltas) < 5:
            return (0, self.normal_momentum_threshold)
        
        deltas_list = list(self.recent_deltas)
        
        if signal > 0:
            # Buy signal: rank current_delta among recent deltas
            deltas_sorted = sorted(deltas_list)
            
            # Check if in top 10% (strong)
            strong_idx = int(self.strong_signal_percentile * (len(deltas_sorted) - 1))
            strong_idx = max(0, min(strong_idx, len(deltas_sorted) - 1))
            strong_threshold = deltas_sorted[strong_idx]
            
            if current_delta >= strong_threshold:
                return (2, self.strong_momentum_threshold)
            
            # Check if in top 30% (normal)
            normal_idx = int(self.normal_signal_percentile * (len(deltas_sorted) - 1))
            normal_idx = max(0, min(normal_idx, len(deltas_sorted) - 1))
            normal_threshold = deltas_sorted[normal_idx]
            
            if current_delta >= normal_threshold:
                return (1, self.normal_momentum_threshold)
            
            return (0, self.normal_momentum_threshold)
        else:
            # Sell signal: rank current_delta among recent deltas
            deltas_sorted = sorted(deltas_list)
            
            # Check if in bottom 10% (strong)
            strong_idx = int((1.0 - self.strong_signal_percentile) * (len(deltas_sorted) - 1))
            strong_idx = max(0, min(strong_idx, len(deltas_sorted) - 1))
            strong_threshold = deltas_sorted[strong_idx]
            
            if current_delta <= strong_threshold:
                return (2, self.strong_momentum_threshold)
            
            # Check if in bottom 30% (normal)
            normal_idx = int((1.0 - self.normal_signal_percentile) * (len(deltas_sorted) - 1))
            normal_idx = max(0, min(normal_idx, len(deltas_sorted) - 1))
            normal_threshold = deltas_sorted[normal_idx]
            
            if current_delta <= normal_threshold:
                return (1, self.normal_momentum_threshold)
            
            return (0, self.normal_momentum_threshold)

    def _check_momentum(self, current_delta: float, signal: int, momentum_threshold: float) -> bool:
        """
        Check if delta momentum aligns with signal direction.
        Uses the provided momentum_threshold (0.3x for strong, 0.5x for normal).
        """
        if signal == 0:
            return False
        
        if signal > 0:
            # Buy signal: require current_delta > threshold * previous_delta
            return current_delta > momentum_threshold * self.prev_delta
        else:
            # Sell signal: require current_delta < threshold * previous_delta
            return current_delta < momentum_threshold * self.prev_delta

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.recent_deltas.clear()
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
        self.recent_deltas.append(delta)
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
        
        # 70th percentile for threshold (proven in #26)
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
        
        # Compute volatility adjustment using 65th percentile (like #26)
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
        
        # Scale bracket levels based on both price volatility and delta volatility
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.6, min(combined_vol_ratio, 1.8))
        
        stop_offset = self.base_stop_offset * combined_vol_ratio
        target_offset = self.base_target_offset * combined_vol_ratio
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Classify signal strength and get appropriate momentum threshold
            signal_strength, momentum_threshold = self._classify_signal_strength(current_delta, signal)
            
            # Only trade if signal strength is at least NORMAL (strength >= 1)
            if signal_strength < 1:
                self.prev_delta = current_delta
                return
            
            # Check momentum confirmation with strength-specific threshold
            momentum_ok = self._check_momentum(current_delta, signal, momentum_threshold)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # recent price action shows upward bias, momentum confirms
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                positive_price_dirs >= len(price_dirs_list) - 1 and
                momentum_ok):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # recent price action shows downward bias, momentum confirms
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  negative_price_dirs >= len(price_dirs_list) - 1 and
                  momentum_ok):
                
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
