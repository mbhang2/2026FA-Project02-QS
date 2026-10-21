"""
An order flow strategy with refined signal quality and improved entry timing.

Key insights from prior trials:

1. Trial #26 (score +5.7681) - BEST SO FAR:
   - Adaptive delta threshold based on delta volatility
   - Combined volatility ratio scaling (price vol * delta vol)
   - Multi-bar confirmation with price direction alignment
   - 70th percentile delta threshold proven optimal
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Simple, elegant, and highly effective

2. Trial #30 (score +2.9879) and #31 (score +2.7319):
   - Added delta trend consistency and regime awareness
   - Added extremeness filtering (top 20%)
   - Both underperformed #26, confirming over-filtering is the problem
   - The core #26 formula is near-optimal and robust

3. Analysis of what makes #26 optimal:
   - The adaptive threshold scaling with delta_vol_ratio is the key
   - Combined volatility ratio for brackets is well-calibrated
   - Multi-bar confirmation with majority-based approach is the sweet spot
   - The simplicity is a feature, not a bug - fewer filters = fewer false rejections
   - Trying to improve #26 with additional constraints has consistently failed

4. New hypothesis for improvement:
   - #26's core formula is optimal for signal detection
   - The issue might be in the bracket sizing or the confirmation logic
   - Instead of adding more filters, refine the existing ones
   - Focus on: better volatility estimation, tighter parameter calibration
   - The 70th percentile threshold is proven - keep it
   - The MA period 10 is proven - keep it
   - The confirmation_bars 2 with majority rule is proven - keep it

This strategy refines #26 by:
1. Keeping all core logic from #26 exactly as is:
   - 70th percentile delta threshold (proven optimal)
   - Adaptive delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8] for bracket scaling
   - Multi-bar confirmation with majority-based rule
   - MA period 10, delta lookback 14, vol lookback 12

2. Subtle refinements to improve robustness:
   - Better handling of edge cases in volatility calculation
   - More stable percentile calculations with explicit bounds
   - Improved numerical stability in delta volatility computation
   - Cleaner session boundary reset logic

3. Fine-tuning volatility scaling:
   - Keep the 65th percentile for price volatility (proven)
   - Keep the combined ratio bounds [0.6, 1.8] (proven)
   - Ensure consistent handling of edge cases
   - Better numerical stability

4. Maintaining all parameters that made #26 work:
   - MA period 10 (trend filter)
   - Delta lookback 14 (distribution measurement)
   - Vol lookback 12 (price volatility)
   - Confirmation bars 2 (multi-bar alignment)
   - Base stop/target offset 0.75

Key hypothesis:
- #26 is near-optimal, and the path to improvement is refinement not addition
- Better numerical stability and edge case handling can improve scores
- Avoiding over-fitting and over-filtering is crucial
- The simple, proven formula should be the foundation
- Small improvements in robustness can compound to better scores

The strategy:
1. Tracks price trend via moving average (MA period 10)
2. Measures delta volatility for adaptive threshold scaling
3. Uses 70th percentile delta threshold (proven optimal)
4. Scales threshold adaptively based on delta volatility
5. Uses multi-bar confirmation with majority-based rule (proven)
6. Filters by price direction momentum alignment
7. Scales brackets based on combined volatility ratio
8. Maintains all proven parameters from #26
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
        """Compute the standard deviation of deltas with numerical stability."""
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
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # and recent price action shows upward bias
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                positive_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # and recent price action shows downward bias
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  negative_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
