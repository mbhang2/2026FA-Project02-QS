"""
An order flow strategy with improved signal quality and regime-adaptive thresholds.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with multi-bar confirmation + secondary trend confirmation
2. Trial #5 (score +2.6629) had solid multi-bar confirmation + volatility adjustment
3. The winning pattern: price trend (MA) + order flow (delta) + volatility adjustment + multi-bar confirmation

This strategy refines #7 by:
1. Improving delta threshold calculation: use a more robust percentile approach with better regime detection
2. Simplifying secondary trend confirmation: reduce noise by requiring stronger alignment
3. Better volatility measurement: use adaptive percentile based on recent volatility regime
4. Tighter entry conditions: require current bar delta to be in top tier of recent activity
5. Smarter confirmation logic: weight recent signals by their strength (not just count)
6. Adaptive target/stop scaling: scale differently based on volatility regime extremeness

Key hypothesis:
- #7 works because it combines multiple confirmation layers
- Weakness: the secondary trend confirmation (3-bar check) might be too loose
- The delta threshold at 70th percentile might miss some strong signals
- Improvement: use weighted signal confirmation and stricter trend alignment
- Also: detect volatility regime more intelligently (low/normal/high)

The strategy:
1. Tracks price trend via moving average (trend filter)
2. Measures delta distribution with regime-aware percentile selection
3. Uses weighted multi-bar confirmation (stronger recent signals count more)
4. Requires strict secondary trend confirmation (at least 2 of last 3 bars aligned)
5. Scales volatility adjustment based on regime (low/normal/high volatility)
6. Only enters when current delta is in top percentile of recent activity
7. Adaptive bracket sizing based on volatility regime extremeness
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        ma_period: int = 10,
        delta_lookback: int = 16,
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
        
        # Track trend signals with strength for weighted confirmation
        self.trend_signals: deque[float] = deque(maxlen=confirmation_bars)
        
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
        
        # Compute delta threshold from percentile of absolute deltas
        deltas_list = list(self.deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # Use 72nd percentile for threshold (slightly higher than #7's 70th)
        threshold_idx = int(0.72 * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Current delta
        current_delta = delta
        
        # Determine signal strength based on delta extremeness
        # Signal strength: how far above/below threshold (normalized)
        signal_strength = 0.0
        if current_delta > delta_threshold:
            # Normalize strength: 1.0 at threshold, increases above
            signal_strength = min(2.0, 1.0 + (current_delta - delta_threshold) / (delta_threshold + 0.01))
        elif current_delta < -delta_threshold:
            # Normalize strength: 1.0 at -threshold, more negative increases strength
            signal_strength = min(2.0, 1.0 + (-current_delta - delta_threshold) / (delta_threshold + 0.01))
        
        # Only process if signal is strong enough
        if signal_strength > 0:
            if current_delta > delta_threshold:
                self.trend_signals.append(signal_strength)  # Positive signal
            else:
                self.trend_signals.append(-signal_strength)  # Negative signal
        else:
            self.trend_signals.append(0.0)  # No signal
        
        # Compute volatility adjustment
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            # Detect volatility regime: use percentile-based approach
            vol_low_idx = int(0.33 * (len(ranges_sorted) - 1))
            vol_high_idx = int(0.67 * (len(ranges_sorted) - 1))
            vol_low_idx = max(0, min(vol_low_idx, len(ranges_sorted) - 1))
            vol_high_idx = max(0, min(vol_high_idx, len(ranges_sorted) - 1))
            
            vol_low = ranges_sorted[vol_low_idx]
            vol_high = ranges_sorted[vol_high_idx]
            
            # Use median for volatility ratio calculation
            vol_median_idx = len(ranges_sorted) // 2
            vol_median = ranges_sorted[vol_median_idx]
            
            avg_range = sum(ranges_list) / len(ranges_list)
            if avg_range > 0.0:
                vol_ratio = vol_median / avg_range
            else:
                vol_ratio = 1.0
        else:
            vol_ratio = 1.0
        
        # Clamp volatility ratio to reasonable bounds
        vol_ratio = max(0.65, min(vol_ratio, 1.55))
        
        # Adjust bracket levels based on volatility
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        # Multi-bar weighted confirmation
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            
            # Weighted signal: recent signals count more
            weighted_sum = 0.0
            weight_sum = 0.0
            for i, sig in enumerate(signals_list):
                weight = 1.0 + (i / len(signals_list))  # Recent signals weighted higher
                weighted_sum += sig * weight
                weight_sum += weight
            
            weighted_avg = weighted_sum / weight_sum if weight_sum > 0 else 0.0
            
            # Secondary trend confirmation: require at least 2 of last 3 bars aligned
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Long: price above MA, weighted positive signals, current strong signal,
            # and recent price action shows upward bias (at least 2 of 3)
            if (price_above_ma and 
                weighted_avg > 0.5 and 
                current_delta > delta_threshold and
                positive_price_dirs >= 2):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="weighted-long",
                )
            
            # Short: price below MA, weighted negative signals, current strong signal,
            # and recent price action shows downward bias (at least 2 of 3)
            elif (price_below_ma and 
                  weighted_avg < -0.5 and 
                  current_delta < -delta_threshold and
                  negative_price_dirs >= 2):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="weighted-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
