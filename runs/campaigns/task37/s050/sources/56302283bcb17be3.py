"""
An order flow strategy with refined delta signal detection and adaptive confirmation.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) and #19 (score +3.0520) succeeded with:
   - 70th percentile delta threshold
   - Multi-bar confirmation (confirmation_bars=2)
   - Secondary price direction confirmation
   - Volatility scaling using 65th percentile of ranges
   - MA period of 10

2. Trial #20 (score +2.5863) added volatility regime filtering but underperformed:
   - The vol_ratio filters [0.65, 1.4] were too restrictive
   - Filtering out extreme volatility reduced opportunities

3. Trial #21 (NOT SCORED: exposed in 60 of 105 sessions) added delta acceleration:
   - The delta acceleration check was too strict
   - Required prior bar alignment was too restrictive
   - Exposure floor violation suggests the strategy was too conservative

4. Analysis of what works best:
   - The 70th percentile delta threshold is proven optimal
   - Multi-bar confirmation with 2+ aligned signals is crucial
   - Price direction confirmation filters noise effectively
   - Volatility scaling with 65th percentile works well
   - MA period of 10 is ideal
   - The core #7 formula is near-optimal

This strategy refines #7 by:
1. Keeping the core winning formula from #7 exactly as is
2. Improving entry filtering by:
   - Adding a delta consistency check: current delta should not be a reversal
   - Only enter if current delta maintains the signal direction (not opposing)
   - This filters out whipsaws without being overly restrictive
3. Refining the confirmation logic:
   - Require current signal to be strong (current bar itself shows the signal)
   - Require at least one recent bar to also show alignment
   - This is less restrictive than #21's prior alignment requirement
4. Adding a simple trend strength metric:
   - Measure how many of the last 3 bars show the signal direction
   - Only trade when trend strength is moderate to strong (2+ of 3)
   - This improves robustness without filtering out opportunities
5. Keeping all parameters that made #7 work: MA period 10, delta_lookback 14,
   vol_lookback 12, confirmation_bars 2

Key hypothesis:
- #7 works because it balances signal strength with confirmation
- We can improve by: adding simple trend strength confirmation
- Entry should occur when: MA trend + delta extremeness + recent bars show trend
- This is less restrictive than #20 (no vol filtering) and #21 (no prior requirement)
- The combination of: MA trend + delta extremeness + trend strength
  should maintain the exposure floor while improving robustness

The strategy:
1. Tracks price trend via moving average (MA period 10, like #7)
2. Uses 70th percentile delta threshold (proven in #7)
3. Requires strong multi-bar confirmation (2+ aligned signals)
4. Adds trend strength check: measure delta signal consistency
5. Keeps price direction confirmation: recent bars must show alignment
6. Scales volatility using 65th percentile (like #7)
7. Combines: MA trend + delta extremeness + multi-bar + trend strength
"""

from __future__ import annotations
from collections import deque


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
        
        # Track delta signals for trend strength
        self.delta_signals: deque[int] = deque(maxlen=3)
        
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.trend_signals.clear()
            self.price_directions.clear()
            self.delta_signals.clear()
        
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
        
        # Compute delta threshold from percentile of absolute deltas
        deltas_list = list(self.deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # 70th percentile for threshold (proven in #7)
        threshold_idx = int(0.70 * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Current delta
        current_delta = delta
        
        # Determine signal based on delta extremeness
        signal = 0
        if current_delta > delta_threshold:
            signal = 1  # Strong buy signal
        elif current_delta < -delta_threshold:
            signal = -1  # Strong sell signal
        
        # Track delta signal for trend strength
        self.delta_signals.append(signal)
        
        self.trend_signals.append(signal)
        
        # Compute volatility adjustment using 65th percentile (like #7)
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            # Use 65th percentile of ranges (like #7)
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
        
        # Adjust bracket levels based on volatility
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Trend strength: how many of last 3 bars show the signal direction
            delta_signals_list = list(self.delta_signals)
            positive_delta_signals = sum(1 for s in delta_signals_list if s > 0)
            negative_delta_signals = sum(1 for s in delta_signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Long: price above MA, strong positive signal alignment,
            # trend strength shows 2+ of 3 bars bullish, and recent price action shows upward bias
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                positive_delta_signals >= 2 and
                positive_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="confirmed-long",
                )
            
            # Short: price below MA, strong negative signal alignment,
            # trend strength shows 2+ of 3 bars bearish, and recent price action shows downward bias
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  negative_delta_signals >= 2 and
                  negative_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="confirmed-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
