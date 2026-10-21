"""
An order flow strategy with dynamic regime detection and adaptive entry timing.

Key insights from prior trials:
1. Trial #5 (score +2.6629, best) succeeded with multi-bar confirmation + volatility adjustment
2. Trial #2 (score +2.4655) had solid trend + volatility logic
3. Trials #3-4 show that over-parameterization hurts performance

This strategy refines #5 by:
1. Simplifying the confirmation logic: use a cleaner voting mechanism
2. Adding regime detection: distinguish high-delta vs low-delta environments
3. Improving delta threshold: use median-based approach instead of percentile
4. Better volatility measurement: use recent volatility to scale entries more dynamically
5. Tighter entry conditions: require stronger alignment between price trend and order flow
6. Adaptive position sizing: scale entry size based on signal strength (still binary for simplicity)

Key hypothesis:
- #5 works because it combines trend filtering with order flow confirmation
- The weakness is that it uses fixed percentile thresholds regardless of market regime
- Markets with high order flow extremes (high volatility, institutional activity) need different thresholds
- A regime-aware approach that adapts the delta threshold to recent market behavior should outperform

The strategy:
1. Tracks price trend via moving average (like #5)
2. Measures delta distribution and regime (high vs normal delta activity)
3. Uses median-based thresholds that adapt to recent delta magnitude
4. Requires strong multi-bar confirmation aligned with price trend
5. Scales volatility adjustment more aggressively when in high-regime
6. Filters out weak signals in low-conviction environments
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
        regime_lookback: int = 20,
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
            regime_lookback: bars to measure regime (high vs normal delta activity)
            confirmation_bars: number of bars that must align with trend
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.regime_lookback = regime_lookback
        self.confirmation_bars = confirmation_bars
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
        # Track closes for moving average
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track deltas for distribution analysis
        self.deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track absolute deltas for regime detection
        self.abs_deltas: deque[float] = deque(maxlen=regime_lookback)
        
        # Track bar ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track trend signals for multi-bar confirmation
        self.trend_signals: deque[int] = deque(maxlen=confirmation_bars)
        
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.abs_deltas.clear()
            self.ranges.clear()
            self.trend_signals.clear()
        
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
        abs_delta = abs(delta)
        self.abs_deltas.append(abs_delta)
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
        
        # Compute delta threshold from median of absolute deltas
        deltas_list = list(self.deltas)
        abs_deltas_list = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas_list)
        median_idx = len(abs_deltas_sorted) // 2
        delta_threshold = abs_deltas_sorted[median_idx]
        
        # Avoid zero threshold
        if delta_threshold < 1.0:
            delta_threshold = 1.0
        
        # Detect regime: high vs normal delta activity
        if len(self.abs_deltas) >= self.regime_lookback:
            regime_abs_deltas = sorted(list(self.abs_deltas))
            regime_75_idx = int(0.75 * (len(regime_abs_deltas) - 1))
            regime_75_idx = max(0, min(regime_75_idx, len(regime_abs_deltas) - 1))
            regime_threshold = regime_abs_deltas[regime_75_idx]
            
            # High regime if recent activity is above 75th percentile
            in_high_regime = abs_delta > regime_threshold
        else:
            in_high_regime = False
        
        # Current delta
        current_delta = delta
        
        # Determine signal based on delta extremeness and direction
        # Adjust threshold based on regime
        adjusted_threshold = delta_threshold
        if in_high_regime:
            # In high regime, require even more extreme delta
            adjusted_threshold = delta_threshold * 1.1
        
        signal = 0
        if current_delta > adjusted_threshold:
            signal = 1  # Strong buy signal
        elif current_delta < -adjusted_threshold:
            signal = -1  # Strong sell signal
        
        self.trend_signals.append(signal)
        
        # Compute volatility adjustment
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            # Use 60th percentile of ranges
            vol_idx = int(0.6 * (len(ranges_sorted) - 1))
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
        vol_ratio = max(0.65, min(vol_ratio, 1.5))
        
        # In high regime, be more aggressive with volatility adjustment
        if in_high_regime:
            vol_ratio = min(vol_ratio * 1.15, 1.5)
        
        # Adjust bracket levels based on volatility
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Long: price above MA, strong positive signal alignment, current signal positive
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="regime-long",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="regime-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
