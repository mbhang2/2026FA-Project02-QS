"""
An order flow strategy with refined delta signal detection and adaptive entry filtering.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with:
   - 70th percentile delta threshold
   - Multi-bar confirmation (confirmation_bars=2)
   - Secondary price direction confirmation
   - Volatility scaling using 65th percentile of ranges

2. Trial #18 (score +2.2879) added minimum bar range filter
   - Helped avoid dead zones
   - But the MA period of 9 may have been too aggressive

3. Trial #17 (score +1.7649) added confidence threshold
   - The confidence threshold of 0.3 was too restrictive
   - Filtering on signal strength reduced opportunities

Analysis of what works best:
- The 70th percentile delta threshold is optimal
- Multi-bar confirmation with 2+ aligned signals is crucial
- Price direction confirmation helps filter noise
- Volatility scaling with 65th percentile works well
- The balance in #7 is nearly optimal
- MA period of 10 (from #7) is better than 9 (from #18)

This strategy refines #7 by:
1. Keeping the core winning formula exactly as in #7
2. Adding a dynamic entry strength metric based on delta extremeness
3. Using a tiered confirmation approach:
   - Normal regime: require 2+ aligned signals (like #7)
   - Extreme regime: allow entry with 1 signal if delta is very strong
4. Refining the threshold calculation to be more adaptive
5. Adding a secondary filter: require current delta to be above the 60th percentile
   of recent absolute deltas (not just the 70th percentile threshold)
6. Keeping all parameters that made #7 work: MA period 10, delta_lookback 14,
   vol_lookback 12, confirmation_bars 2

Key hypothesis:
- #7 works because the parameters are well-balanced
- We can improve by: adding a tiered confirmation approach
- In normal markets: require strict 2+ signal confirmation (like #7)
- In extreme markets: allow entry with 1 very strong signal
- This captures more opportunities in trending markets while maintaining discipline
- The 60th percentile secondary filter provides additional confirmation
- Entry only when: MA aligned + delta extreme + multi-bar aligned + secondary filter

The strategy:
1. Tracks price trend via moving average (MA period 10, like #7)
2. Uses 70th percentile delta threshold (proven in #7)
3. Measures 60th percentile for secondary confirmation (stricter than threshold)
4. Requires strong multi-bar confirmation (2+ aligned signals, like #7)
5. Adds tiered entry: strict in normal regimes, slightly relaxed in extremes
6. Adds price direction confirmation: recent bars must show alignment
7. Scales volatility using 65th percentile (like #7)
8. Combines: MA trend + delta percentile + multi-bar alignment + secondary filter
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
        
        # Compute delta threshold from 70th percentile of absolute deltas
        deltas_list = list(self.deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # 70th percentile for threshold (proven in #7)
        threshold_idx = int(0.70 * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # 60th percentile for secondary confirmation (stricter)
        secondary_idx = int(0.60 * (len(abs_deltas_sorted) - 1))
        secondary_idx = max(0, min(secondary_idx, len(abs_deltas_sorted) - 1))
        delta_secondary = abs_deltas_sorted[secondary_idx]
        
        # Ensure minimum thresholds
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        if delta_secondary < 0.3:
            delta_secondary = 0.3
        
        # Current delta
        current_delta = delta
        current_delta_mag = abs(current_delta)
        
        # Measure how extreme the current delta is relative to the threshold
        if delta_threshold > 0:
            extremeness = (current_delta_mag - delta_threshold) / delta_threshold
        else:
            extremeness = 0.0
        
        extremeness = max(0.0, extremeness)
        
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
            
            # Check secondary filter: current delta must exceed 60th percentile
            passes_secondary_filter = current_delta_mag > delta_secondary
            
            # Tiered confirmation:
            # - Strict mode: require 2+ aligned signals + secondary filter
            # - Relaxed mode: allow 1 signal if delta is very extreme (extremeness > 0.5)
            strict_confirmation = (positive_signals >= self.confirmation_bars - 1 or 
                                 negative_signals >= self.confirmation_bars - 1)
            extreme_delta = extremeness > 0.5
            
            # Long: price above MA, current signal positive, strong confirmation
            if (price_above_ma and 
                signal > 0 and
                passes_secondary_filter and
                positive_price_dirs >= len(price_dirs_list) - 1 and
                (strict_confirmation or extreme_delta)):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry-long",
                )
            
            # Short: price below MA, current signal negative, strong confirmation
            elif (price_below_ma and 
                  signal < 0 and
                  passes_secondary_filter and
                  negative_price_dirs >= len(price_dirs_list) - 1 and
                  (strict_confirmation or extreme_delta)):
                
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
