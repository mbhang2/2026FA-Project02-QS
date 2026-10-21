"""
An order flow strategy with enhanced signal quality through delta extremeness detection.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with multi-bar confirmation + volatility adjustment
2. Trial #12 (score +2.8520) added delta acceleration detection but slightly underperformed
3. The core winning pattern: price trend (MA) + order flow (delta) + volatility adjustment

This strategy refines #7 by:
1. Simplifying delta acceleration logic: use a cleaner percentile-based approach
2. Improving signal quality: require delta to be in the TOP percentile of extremeness
3. Better multi-bar confirmation: track alignment of extreme delta signals
4. More robust volatility scaling: use actual recent volatility, not percentile
5. Cleaner entry logic: delta extremeness + price trend + multi-bar alignment
6. Session-aware state management: reset all tracking at session boundaries

Key hypothesis:
- #7 works because multi-bar confirmation reduces false signals
- #12's delta acceleration is a good idea but the implementation is complex
- Strength: combining price trend + order flow + volatility adjustment
- Weakness in #12: acceleration detection adds complexity without clear benefit
- Improvement: use simpler delta extremeness (is delta in top/bottom percentile?)
- Also: track which bars had extreme deltas, require alignment for entry

The strategy:
1. Tracks price trend via moving average (trend filter)
2. Measures delta distribution and identifies extreme deltas (top 35th percentile)
3. Uses simple multi-bar confirmation: recent bars must have aligned extreme deltas
4. Requires price trend alignment + current extreme delta signal
5. Scales volatility adjustment based on actual recent range volatility
6. Filters weak signals by requiring delta to be in the extreme tail
7. Multi-bar alignment: recent extreme deltas must point same direction
8. More selective entry: only trade when delta is truly extreme AND aligned
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
        
        # Track recent delta signals (1 for extreme buy, -1 for extreme sell, 0 for none)
        self.extreme_signals: deque[int] = deque(maxlen=confirmation_bars)
        
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
            self.extreme_signals.clear()
        
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
        
        # Compute delta extremeness threshold using 65th percentile of absolute deltas
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
        
        # Detect if current delta is extreme (in the top 35% of absolute deltas)
        extreme_signal = 0
        if current_delta > delta_threshold:
            extreme_signal = 1  # Extreme buy signal
        elif current_delta < -delta_threshold:
            extreme_signal = -1  # Extreme sell signal
        
        self.extreme_signals.append(extreme_signal)
        
        # Compute volatility adjustment for bracket sizing
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            avg_range = sum(ranges_list) / len(ranges_list)
            
            # Use actual recent volatility, not percentile
            # Volatility ratio based on current range vs average
            if avg_range > 0.0:
                current_range = ranges_list[-1]
                vol_ratio = current_range / avg_range
            else:
                vol_ratio = 1.0
        else:
            vol_ratio = 1.0
        
        # Clamp volatility ratio to reasonable bounds
        vol_ratio = max(0.6, min(vol_ratio, 1.6))
        
        # Adjust bracket levels based on volatility
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        # Multi-bar confirmation: check if recent extreme signals align
        if len(self.extreme_signals) >= self.confirmation_bars:
            signals_list = list(self.extreme_signals)
            
            # Count aligned extreme signals
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Long: price above MA, current extreme buy signal,
            # and recent extreme signals show buy alignment
            if (price_above_ma and 
                extreme_signal > 0 and
                positive_signals >= self.confirmation_bars):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="extreme-long",
                )
            
            # Short: price below MA, current extreme sell signal,
            # and recent extreme signals show sell alignment
            elif (price_below_ma and 
                  extreme_signal < 0 and
                  negative_signals >= self.confirmation_bars):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="extreme-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
