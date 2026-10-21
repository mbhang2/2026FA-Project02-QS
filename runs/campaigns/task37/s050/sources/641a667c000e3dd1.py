"""
An order flow strategy with improved signal quality and adaptive confirmation.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with multi-bar confirmation + volatility adjustment
2. Trial #10 (score +2.4468) added weighted signal strengths but slightly underperformed
3. The core winning pattern: price trend (MA) + order flow (delta) + volatility adjustment

This strategy refines #7 by:
1. Improving delta threshold with better percentile selection (use 65th instead of 70th)
2. Adding a simpler, more robust multi-bar confirmation (just count aligned signals)
3. Better entry filtering: require current signal to be strong AND recent bars aligned
4. Volatility-aware bracket sizing: use 65th percentile of ranges (matching #5's success)
5. Cleaner state management: track only essential signal information
6. Adaptive entry requirements: scale confirmation strictness based on signal strength

Key hypothesis:
- #7 works because it combines price trend + order flow + volatility effectively
- #5 (score +2.6629) succeeded with 65th percentile volatility scaling
- Weakness in #7: 70th percentile delta threshold may be too strict
- Weakness in #10: weighted signal strength adds complexity without clear benefit
- Improvement: use 65th percentile delta threshold (more permissive, like #5)
- Also: simpler confirmation logic that's easier to tune and more stable

The strategy:
1. Tracks price trend via moving average (trend filter)
2. Measures delta distribution and uses 65th percentile for threshold
3. Uses simple multi-bar confirmation (recent signals must align)
4. Requires price trend alignment + strong current delta signal
5. Scales volatility adjustment using 65th percentile of ranges (proven in #5)
6. Requires strong delta signal (>1.0x threshold) for entry
7. Multi-bar alignment: all recent signals must point same direction
8. Cleaner logic: remove weighted complexity, focus on signal alignment
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
        
        # Track recent delta signals (1 for buy, -1 for sell, 0 for none)
        self.delta_signals: deque[int] = deque(maxlen=confirmation_bars)
        
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
        
        # Compute delta threshold using 65th percentile (proven in #5)
        deltas_list = list(self.deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        threshold_idx = int(0.65 * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Current delta
        current_delta = delta
        
        # Generate signal based on delta strength
        # Require delta to be strong (>1.0x threshold) for entry
        signal = 0
        if current_delta > delta_threshold * 1.0:
            signal = 1  # Buy signal
        elif current_delta < -delta_threshold * 1.0:
            signal = -1  # Sell signal
        
        self.delta_signals.append(signal)
        
        # Compute volatility adjustment for bracket sizing
        # Use 65th percentile of ranges (matching #5's success)
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
        
        # Adjust bracket levels based on volatility
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        # Multi-bar confirmation: check if recent signals align
        if len(self.delta_signals) >= self.confirmation_bars:
            signals_list = list(self.delta_signals)
            
            # Count aligned signals
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Long: price above MA, current strong buy signal,
            # and all recent signals show alignment
            if (price_above_ma and 
                signal > 0 and
                positive_signals >= self.confirmation_bars):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="aligned-long",
                )
            
            # Short: price below MA, current strong sell signal,
            # and all recent signals show alignment
            elif (price_below_ma and 
                  signal < 0 and
                  negative_signals >= self.confirmation_bars):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="aligned-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
