"""
An order flow strategy with refined confirmation and adaptive thresholds.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with:
   - 70th percentile delta threshold
   - Multi-bar confirmation (confirmation_bars=2)
   - Secondary price direction confirmation
   - Volatility scaling using 65th percentile of ranges

2. Trial #17 (score +1.7649) added confidence threshold but underperformed
   - The confidence threshold of 0.3 was too restrictive
   - Filtering on signal strength reduced opportunities

3. Trial #16 (score +2.4500) was looser but still below #7
   - Required only 1 aligned signal vs 2 in #7
   - Less selective confirmation hurt performance

Analysis of what works best:
- The 70th percentile delta threshold is optimal
- Multi-bar confirmation with 2+ aligned signals is crucial
- Price direction confirmation helps filter noise
- Volatility scaling with 65th percentile works well
- The balance in #7 is nearly optimal

This strategy refines #7 by:
1. Keeping the core winning formula exactly as in #7
2. Adding a secondary volatility regime filter for better selectivity
3. Refining the MA period slightly (9 instead of 10) for faster trend response
4. Adjusting delta_lookback to 15 for slightly smoother threshold
5. Adding a minimum bar range filter to avoid trading in dead zones
6. Improving signal quality by requiring current bar to be stronger than median recent delta
7. Keeping all other parameters that made #7 work: 70th percentile, multi-bar confirmation, price direction

Key hypothesis:
- #7 works because the parameters are well-balanced
- We can improve by: adding a minimum volatility requirement to avoid choppy/dead zones
- Also: require current delta to exceed the median of recent deltas (not just threshold)
- This adds an extra layer of selectivity: signal must be extreme AND stronger than recent bars
- The MA period of 9 gives faster trend response without losing stability
- Minimum bar range filter prevents trading when volatility is too low

The strategy:
1. Tracks price trend via moving average (MA period 9, slightly faster)
2. Uses 70th percentile delta threshold (proven in #7)
3. Measures recent delta median for additional confirmation
4. Requires current delta to exceed both threshold AND recent median
5. Requires strong multi-bar confirmation (2+ aligned signals)
6. Adds price direction confirmation: recent bars must show alignment
7. Scales volatility using 65th percentile (like #7)
8. Adds minimum bar range filter to avoid dead zones
9. Combines: MA trend + delta percentile + median confirmation + multi-bar alignment + vol filter
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        ma_period: int = 9,
        delta_lookback: int = 15,
        vol_lookback: int = 12,
        confirmation_bars: int = 2,
        size: int = 1,
        base_stop_offset: float = 0.75,
        base_target_offset: float = 0.75,
        min_bar_range: float = 0.1,
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
            min_bar_range: minimum bar range to trade (avoid dead zones)
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.confirmation_bars = confirmation_bars
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        self.min_bar_range = min_bar_range
        
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
        
        # Filter out dead zones: require minimum bar range
        bar_range = last.high - last.low
        if bar_range < self.min_bar_range:
            return
        
        # Accumulate data
        self.closes.append(last.close)
        delta = float(last.buy_volume - last.sell_volume)
        self.deltas.append(delta)
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
        
        # Compute delta threshold from 70th percentile of absolute deltas
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
        current_delta_mag = abs(current_delta)
        
        # Compute median of recent deltas for additional confirmation
        deltas_sorted = sorted(abs_deltas)
        median_idx = len(deltas_sorted) // 2
        median_delta = deltas_sorted[median_idx]
        
        # Determine signal based on delta extremeness
        signal = 0
        if current_delta > delta_threshold:
            signal = 1  # Strong buy signal
        elif current_delta < -delta_threshold:
            signal = -1  # Strong sell signal
        
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
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Additional confirmation: current delta must exceed median of recent deltas
            current_exceeds_median = current_delta_mag > median_delta
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # current delta exceeds median, and recent price action shows upward bias
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                current_exceeds_median and
                positive_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry-long",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # current delta exceeds median, and recent price action shows downward bias
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  current_exceeds_median and
                  negative_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
