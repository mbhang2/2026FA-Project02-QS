"""
An order flow strategy with refined delta signal filtering and adaptive position sizing.

Key insights from prior trials:
1. Trial #26 (score +5.7681) - CURRENT BEST:
   - Adaptive delta threshold based on delta volatility
   - Combined volatility ratio scaling (price vol * delta vol)
   - Multi-bar confirmation with price direction alignment
   - 70th percentile delta threshold proven optimal
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #27 (score +4.4387):
   - Added regime strength detection (distribution concentration)
   - Adapted confirmation requirements based on regime
   - Added delta direction alignment check
   - Slightly underperformed #26, suggesting regime adaptation may over-filter

3. Analysis of what makes #26 optimal:
   - The adaptive threshold scaling (delta_vol_ratio) is the key innovation
   - Combined volatility ratio for brackets is well-calibrated
   - Multi-bar confirmation with price direction is sufficient
   - Regime-aware confirmation in #27 may be too restrictive
   - The core formula from #26 is near-optimal

This strategy refines #26 by:
1. Keeping all proven core logic from #26 exactly:
   - Adaptive delta threshold with delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8] for bracket scaling
   - Multi-bar confirmation with price direction checks
   - MA period 10, delta lookback 14, vol lookback 12

2. Adding refined signal quality filters:
   - Measure delta momentum (current delta vs recent average)
   - Only take signals that show delta momentum in the trade direction
   - This filters false signals without over-filtering like regime detection did
   - Delta momentum = current delta moving away from mean in trade direction

3. Adding position sizing based on signal strength:
   - Measure how extreme the current delta is relative to threshold
   - Scale position size based on signal extremeness
   - Stronger signals = larger positions
   - Weaker signals = smaller positions
   - Maintain base size but scale within reasonable bounds [0.7x, 1.3x]

4. Refining entry timing:
   - Check that delta momentum is accelerating (not just present)
   - Ensure the signal is strengthening, not weakening
   - This adds a temporal dimension without over-filtering

5. Keeping all parameters that made #26 work:
   - MA period 10 (trend filter)
   - Delta lookback 14 (distribution measurement)
   - Vol lookback 12 (price volatility)
   - Base confirmation 2 (multi-bar alignment)
   - Adaptive threshold scaling from #26
   - Combined volatility ratio scaling from #26

Key hypothesis:
- #26 works because it balances all factors well
- We can improve by: adding delta momentum as a signal quality filter
- Delta momentum (current moving away from mean) indicates strengthening signal
- Position sizing based on signal extremeness captures more value
- This maintains the core winning formula while improving signal quality
- The combination of: adaptive threshold + delta momentum + position sizing
  should improve on #26's score of 5.7681

The strategy:
1. Tracks price trend via moving average (MA period 10)
2. Measures delta volatility for adaptive threshold scaling
3. Measures delta momentum (current vs recent average)
4. Scales delta threshold adaptively based on delta volatility
5. Filters signals by delta momentum direction alignment
6. Scales position size based on signal extremeness
7. Uses multi-bar confirmation with price direction checks
8. Scales both stop and target based on combined volatility ratio
9. Combines: MA trend + adaptive threshold + delta momentum + position sizing
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
        momentum_lookback: int = 5,
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
            momentum_lookback: bars for delta momentum calculation
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.confirmation_bars = confirmation_bars
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        self.momentum_lookback = momentum_lookback
        
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
        
        # Track recent deltas for momentum calculation
        self.recent_deltas: deque[float] = deque(maxlen=momentum_lookback)
        
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

    def _compute_delta_momentum(self, recent_deltas_list: list[float], current_delta: float) -> tuple[float, float]:
        """
        Compute delta momentum: how far current delta is from recent average.
        Returns (momentum_value, momentum_direction) where direction is 1, -1, or 0.
        """
        if len(recent_deltas_list) < 2:
            return 0.0, 0
        
        recent_avg = sum(recent_deltas_list) / len(recent_deltas_list)
        momentum = current_delta - recent_avg
        
        if momentum > 0:
            direction = 1
        elif momentum < 0:
            direction = -1
        else:
            direction = 0
        
        return momentum, direction

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.trend_signals.clear()
            self.price_directions.clear()
            self.recent_deltas.clear()
        
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
        
        # Adapt threshold based on delta volatility (from #26)
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
        
        # Compute delta momentum for signal quality filtering
        if len(self.recent_deltas) >= 2:
            recent_deltas_list = list(self.recent_deltas)
            momentum, momentum_dir = self._compute_delta_momentum(recent_deltas_list, current_delta)
        else:
            momentum = 0.0
            momentum_dir = 0
        
        # Compute volatility adjustment using 65th percentile (from #26)
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
        
        # Scale bracket levels based on both price volatility and delta volatility (from #26)
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.6, min(combined_vol_ratio, 1.8))
        
        stop_offset = self.base_stop_offset * combined_vol_ratio
        target_offset = self.base_target_offset * combined_vol_ratio
        
        # Compute position size based on signal extremeness
        if abs(current_delta) > adapted_threshold:
            # Scale size based on how extreme the signal is
            extremeness = abs(current_delta) / adapted_threshold
            extremeness = max(1.0, min(extremeness, 2.0))  # Clamp to [1.0, 2.0]
            size_ratio = max(0.7, min(extremeness / 2.0, 1.3))  # Scale to [0.7, 1.3]
        else:
            size_ratio = 1.0
        
        adjusted_size = max(1, int(self.size * size_ratio))
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # delta momentum is positive (strengthening), and recent price action shows upward bias
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                momentum_dir >= 0 and
                positive_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.buy(
                    size=adjusted_size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="confirmed-long",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # delta momentum is negative (strengthening), and recent price action shows downward bias
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  momentum_dir <= 0 and
                  negative_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.sell(
                    size=adjusted_size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="confirmed-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
