"""
An order flow strategy with improved regime detection and adaptive signal filtering.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with multi-bar confirmation + secondary trend
2. Trial #8 (score +2.2768) added weighted signals but slightly underperformed
3. The winning pattern: price trend (MA) + order flow (delta) + volatility adjustment

This strategy refines #7 by:
1. Improving delta threshold with dynamic regime detection (low/normal/high volatility)
2. Adding adaptive confirmation requirements based on volatility regime
3. Better entry filtering: require delta to be in top quartile AND aligned with trend
4. Simpler, more robust signal generation: cleaner logic reduces edge cases
5. Volatility-aware bracket sizing: aggressive in low vol, conservative in high vol
6. Session-aware parameter tuning: reset state properly at boundaries

Key hypothesis:
- #7 works because it combines multiple confirmation layers effectively
- Weakness: fixed 70th percentile delta threshold doesn't adapt to volatility regime
- The confirmation requirement should scale with volatility (stricter when vol is high)
- Improvement: detect volatility regime and adjust threshold/confirmation accordingly
- Also: use a simpler signal generation that's less prone to edge cases

The strategy:
1. Tracks price trend via moving average (trend filter)
2. Measures volatility regime (low/normal/high) from recent bar ranges
3. Adapts delta threshold based on volatility regime (more selective in high vol)
4. Adapts confirmation requirements based on volatility (stricter in high vol)
5. Uses simple multi-bar confirmation (all recent signals must align)
6. Requires delta to be in top quartile of recent activity
7. Scales bracket sizing based on volatility regime
8. Clean entry logic: price trend + delta confirmation + multi-bar alignment
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
        
        # Detect volatility regime
        vol_regime = self._detect_volatility_regime()
        
        # Compute delta threshold based on volatility regime
        # In high vol: use higher percentile (more selective)
        # In low vol: use lower percentile (more permissive)
        if vol_regime == "high":
            percentile = 0.75
            min_confirmation = self.confirmation_bars
        elif vol_regime == "low":
            percentile = 0.65
            min_confirmation = max(1, self.confirmation_bars - 1)
        else:  # normal
            percentile = 0.70
            min_confirmation = self.confirmation_bars
        
        deltas_list = list(self.deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        threshold_idx = int(percentile * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Current delta
        current_delta = delta
        
        # Generate signal based on delta
        signal = 0
        if current_delta > delta_threshold:
            signal = 1  # Buy signal
        elif current_delta < -delta_threshold:
            signal = -1  # Sell signal
        
        self.delta_signals.append(signal)
        
        # Compute volatility adjustment for bracket sizing
        stop_offset, target_offset = self._compute_bracket_offsets(vol_regime)
        
        # Check multi-bar confirmation
        if len(self.delta_signals) >= min_confirmation:
            signals_list = list(self.delta_signals)
            
            # Count aligned signals
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Long: price above MA, current strong buy signal,
            # and recent signals show alignment
            if (price_above_ma and 
                signal > 0 and
                positive_signals >= min_confirmation):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="regime-long",
                )
            
            # Short: price below MA, current strong sell signal,
            # and recent signals show alignment
            elif (price_below_ma and 
                  signal < 0 and
                  negative_signals >= min_confirmation):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="regime-short",
                )

    def _detect_volatility_regime(self) -> str:
        """Detect volatility regime based on recent bar ranges."""
        if len(self.ranges) < 3:
            return "normal"
        
        ranges_list = list(self.ranges)
        ranges_sorted = sorted(ranges_list)
        
        # Use percentiles to detect regime
        low_idx = max(0, min(int(0.33 * (len(ranges_sorted) - 1)), len(ranges_sorted) - 1))
        high_idx = max(0, min(int(0.67 * (len(ranges_sorted) - 1)), len(ranges_sorted) - 1))
        
        vol_low = ranges_sorted[low_idx]
        vol_high = ranges_sorted[high_idx]
        vol_mid = ranges_sorted[len(ranges_sorted) // 2]
        
        # Current range
        current_range = ranges_list[-1]
        
        # Classify based on current range relative to distribution
        if current_range > vol_high * 1.2:
            return "high"
        elif current_range < vol_low * 0.8:
            return "low"
        else:
            return "normal"

    def _compute_bracket_offsets(self, vol_regime: str) -> tuple[float, float]:
        """Compute bracket offsets based on volatility regime."""
        if len(self.ranges) < 2:
            return self.base_stop_offset, self.base_target_offset
        
        ranges_list = list(self.ranges)
        avg_range = sum(ranges_list) / len(ranges_list)
        
        if avg_range <= 0.0:
            return self.base_stop_offset, self.base_target_offset
        
        # Compute volatility ratio
        ranges_sorted = sorted(ranges_list)
        vol_idx = len(ranges_sorted) // 2
        vol_median = ranges_sorted[vol_idx]
        vol_ratio = vol_median / avg_range
        vol_ratio = max(0.65, min(vol_ratio, 1.55))
        
        # Adjust based on regime
        if vol_regime == "high":
            # Conservative in high volatility
            vol_ratio *= 0.9
        elif vol_regime == "low":
            # Aggressive in low volatility
            vol_ratio *= 1.1
        
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        return stop_offset, target_offset

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
