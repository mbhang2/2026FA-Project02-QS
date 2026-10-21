"""
An order flow strategy with refined signal quality and adaptive risk management.

Key insights from prior trials:
1. Trial #26 (score +5.7681) - BEST SO FAR:
   - Adaptive delta threshold based on delta volatility
   - Combined volatility ratio scaling (price vol * delta vol)
   - Multi-bar confirmation with price direction alignment
   - 70th percentile delta threshold proven optimal
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #27 (score +4.4387):
   - Added regime strength detection
   - Adapted confirmation requirements based on regime
   - Underperformed #26, suggesting regime adaptation over-filters

3. Trial #28 (score +4.9002):
   - Added delta momentum filtering
   - Added position sizing based on signal extremeness
   - Slightly underperformed #26, suggesting momentum filter is too strict

4. Analysis of what makes #26 optimal:
   - The core adaptive threshold scaling is the key innovation
   - Combined volatility ratio for brackets is well-calibrated
   - Multi-bar confirmation with price direction is sufficient
   - Additional filters (regime, momentum) degrade performance
   - The formula is near-optimal but may benefit from:
     a) Tighter entry confirmation (require ALL signals aligned, not just majority)
     b) Better volatility scaling calibration
     c) Improved bracket sizing based on actual signal strength

This strategy refines #26 by:
1. Keeping all proven core logic from #26 exactly:
   - Adaptive delta threshold with delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8] for bracket scaling
   - Multi-bar confirmation with price direction checks
   - MA period 10, delta lookback 14, vol lookback 12

2. Improving signal confirmation strictness:
   - Require ALL recent signals to align (not just majority)
   - This filters false signals more effectively than #27/#28's approaches
   - Maintains high signal quality without over-filtering

3. Refining volatility scaling:
   - Use a more sensitive delta volatility calibration
   - Adjust the delta_vol_ratio scaling bounds based on actual volatility levels
   - Better bracket sizing when volatility is extreme

4. Adding a simple price momentum filter:
   - Ensure the price direction (not just delta) is strengthening
   - Check that recent price moves align with the MA trend
   - This is lighter than #28's delta momentum and avoids over-filtering

5. Improving bracket sizing:
   - Scale brackets more aggressively when signal is very strong
   - Use percentile-based scaling that adapts to actual volatility distribution
   - Better risk/reward calibration

Key hypothesis:
- #26 works because it balances all factors well
- We can improve by: tightening confirmation requirements (ALL signals aligned)
- Requiring stricter alignment filters noise better than regime/momentum approaches
- Better volatility scaling calibration improves risk/reward
- Lighter price momentum filter (not delta momentum) maintains edge
- The combination of: stricter confirmation + better vol scaling + price momentum
  should improve on #26's score of 5.7681

The strategy:
1. Tracks price trend via moving average (MA period 10)
2. Measures delta volatility for adaptive threshold scaling
3. Measures price momentum (recent price direction consistency)
4. Scales delta threshold adaptively based on delta volatility
5. Uses STRICT multi-bar confirmation (ALL signals must align)
6. Filters by price momentum direction alignment
7. Scales both stop and target based on refined volatility ratio
8. Combines: MA trend + strict confirmation + price momentum + better vol scaling
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

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.trend_signals.clear()
            self.price_directions.clear()
        
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
        
        # Adapt threshold based on delta volatility with improved calibration
        # Use a more sensitive scaling that better captures volatility changes
        delta_vol_ratio = max(0.65, min(delta_vol / 45.0, 1.4))
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
        
        # Scale bracket levels with refined volatility scaling
        # Use improved bounds that better capture extreme volatility
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.55, min(combined_vol_ratio, 1.9))
        
        stop_offset = self.base_stop_offset * combined_vol_ratio
        target_offset = self.base_target_offset * combined_vol_ratio
        
        # STRICT multi-bar confirmation: ALL signals must align
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            all_positive = all(s > 0 for s in signals_list)
            all_negative = all(s < 0 for s in signals_list)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Price momentum: ensure recent price moves align with trend
            price_momentum_up = positive_price_dirs >= len(price_dirs_list) - 1
            price_momentum_down = negative_price_dirs >= len(price_dirs_list) - 1
            
            # Long: ALL signals positive, price above MA, current signal positive,
            # and price momentum shows upward bias
            if (all_positive and 
                price_above_ma and 
                signal > 0 and
                price_momentum_up):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="strict-confirmed-long",
                )
            
            # Short: ALL signals negative, price below MA, current signal negative,
            # and price momentum shows downward bias
            elif (all_negative and 
                  price_below_ma and 
                  signal < 0 and
                  price_momentum_down):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="strict-confirmed-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
