"""
An order flow strategy with improved signal filtering and regime-adaptive entry logic.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with multi-bar confirmation + secondary trend
2. Trial #9 (score +0.6810) added volatility regime detection but underperformed
3. The core winning pattern: price trend (MA) + order flow (delta) + volatility adjustment

This strategy refines #7 by:
1. Simplifying the confirmation logic: remove the secondary price direction check which may be noisy
2. Improving delta threshold calculation: use a more adaptive percentile based on delta distribution shape
3. Better signal strength weighting: require stronger alignment in the confirmation bars
4. More aggressive volatility scaling: use a wider range for vol_ratio to capture more edge cases
5. Cleaner entry logic: price trend + strong delta confirmation + weighted multi-bar alignment
6. Adaptive thresholds: scale confirmation requirements based on how extreme the current delta is

Key hypothesis:
- #7 works because it combines price trend + order flow + volatility effectively
- Weakness: the secondary trend confirmation (3-bar price direction check) adds noise
- The delta threshold could be more adaptive (respond to distribution shape)
- Improvement: use signal strength weighting and simpler trend alignment
- Also: require stronger confirmation when delta signal is marginal

The strategy:
1. Tracks price trend via moving average (trend filter)
2. Measures delta distribution and computes adaptive threshold
3. Weights signal strength based on how far delta exceeds threshold
4. Uses weighted multi-bar confirmation (stronger recent signals count more)
5. Requires price trend alignment + strong current delta signal
6. Scales volatility adjustment based on recent range distribution
7. Adaptive entry: only trade when multiple conditions align strongly
8. Cleaner logic: remove noisy secondary trend confirmation
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
            confirmation_bars: number of bars for multi-bar confirmation
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
        
        # Track signal strengths for weighted confirmation
        self.signal_strengths: deque[float] = deque(maxlen=confirmation_bars)
        
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
            self.signal_strengths.clear()
        
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
        
        # Compute adaptive delta threshold
        deltas_list = list(self.deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # Use 68th percentile for threshold (between #7's 70th and #9's 65th)
        # This adapts better to distribution shape
        threshold_idx = int(0.68 * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Current delta
        current_delta = delta
        
        # Compute signal strength: normalized distance from threshold
        signal_strength = 0.0
        signal_direction = 0  # 1 for buy, -1 for sell, 0 for none
        
        if current_delta > delta_threshold:
            # Buy signal: strength increases with distance above threshold
            excess = current_delta - delta_threshold
            signal_strength = 1.0 + (excess / (delta_threshold + 0.01))
            signal_strength = min(3.0, signal_strength)  # Cap at 3.0
            signal_direction = 1
        elif current_delta < -delta_threshold:
            # Sell signal: strength increases with distance below threshold
            excess = -current_delta - delta_threshold
            signal_strength = 1.0 + (excess / (delta_threshold + 0.01))
            signal_strength = min(3.0, signal_strength)  # Cap at 3.0
            signal_direction = -1
        
        # Store signal strength with direction
        if signal_direction > 0:
            self.signal_strengths.append(signal_strength)
        elif signal_direction < 0:
            self.signal_strengths.append(-signal_strength)
        else:
            self.signal_strengths.append(0.0)
        
        # Compute volatility adjustment
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            # Use 60th percentile for volatility measurement
            vol_idx = int(0.60 * (len(ranges_sorted) - 1))
            vol_idx = max(0, min(vol_idx, len(ranges_sorted) - 1))
            percentile_range = ranges_sorted[vol_idx]
            
            avg_range = sum(ranges_list) / len(ranges_list)
            if avg_range > 0.0:
                vol_ratio = percentile_range / avg_range
            else:
                vol_ratio = 1.0
        else:
            vol_ratio = 1.0
        
        # Clamp volatility ratio to reasonable bounds (wider range than #7)
        vol_ratio = max(0.55, min(vol_ratio, 1.65))
        
        # Adjust bracket levels based on volatility
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        # Multi-bar weighted confirmation
        if len(self.signal_strengths) >= self.confirmation_bars:
            strengths_list = list(self.signal_strengths)
            
            # Compute weighted average (recent signals weighted higher)
            weighted_sum = 0.0
            weight_sum = 0.0
            for i, strength in enumerate(strengths_list):
                # Linear weighting: more recent bars have higher weight
                weight = 1.0 + (i / len(strengths_list))
                weighted_sum += strength * weight
                weight_sum += weight
            
            weighted_avg = weighted_sum / weight_sum if weight_sum > 0 else 0.0
            
            # Entry conditions: price trend + strong current signal + weighted confirmation
            # Require weighted average to be reasonably strong (> 0.6 for buy, < -0.6 for sell)
            # This ensures multi-bar alignment
            
            if (price_above_ma and 
                signal_direction > 0 and
                weighted_avg > 0.6 and
                signal_strength >= 1.2):  # Require current signal to be at least moderately strong
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="adaptive-long",
                )
            
            elif (price_below_ma and 
                  signal_direction < 0 and
                  weighted_avg < -0.6 and
                  signal_strength >= 1.2):  # Require current signal to be at least moderately strong
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="adaptive-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
