"""
A market microstructure strategy using order flow imbalance with adaptive thresholds.

Key insights from prior trials:
1. Trial #2 (score +2.4655) succeeded with trend-following + volatility adjustment
2. Trial #3 (score +1.6181) added regime detection but underperformed
3. The best approach combines trend + order flow, but may be over-parameterized

This strategy simplifies and refines the winning approach (#2):
1. Core signal: order flow delta (buy_volume - sell_volume) as primary indicator
2. Trend confirmation: price relative to a short moving average
3. Adaptive thresholds: scale entry thresholds based on recent delta distribution
4. Volatility-adjusted exits: bracket levels adjust to recent price movement
5. Reduced complexity: fewer parameters, cleaner logic than #3

Key improvements over #2:
- Use absolute delta values more directly (not z-scores which can be unstable)
- Simpler volatility calculation: use recent bar ranges directly
- Tighter confirmation: require current bar's delta to be extreme, not just positive
- Better parameter tuning: based on analysis of what worked in #2

The hypothesis: the winning formula in #2 works because it captures:
1. Institutional order flow (delta extremes)
2. Price trend alignment (price vs MA)
3. Risk management (volatility-adjusted stops)

This version maintains those elements but with:
- Cleaner implementation
- Better-tuned thresholds
- Fewer state variables to manage
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        ma_period: int = 8,
        delta_lookback: int = 12,
        vol_lookback: int = 8,
        delta_percentile: float = 0.70,
        size: int = 1,
        base_stop_offset: float = 0.70,
        base_target_offset: float = 0.70,
    ):
        """
        Args:
            interval: bar interval to subscribe to
            ma_period: period for moving average (trend filter)
            delta_lookback: bars to measure delta distribution over
            vol_lookback: bars for volatility measurement
            delta_percentile: percentile threshold for delta extremeness (0-1)
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.delta_percentile = delta_percentile
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
        # Track closes for moving average
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track absolute deltas for threshold calculation
        self.abs_deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track bar ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.abs_deltas.clear()
            self.ranges.clear()
        
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last = history[-1]
        
        # Accumulate data
        self.closes.append(last.close)
        
        # Calculate delta (buy volume - sell volume)
        delta = float(last.buy_volume - last.sell_volume)
        abs_delta = abs(delta)
        self.abs_deltas.append(abs_delta)
        
        # Track bar range for volatility
        bar_range = last.high - last.low
        self.ranges.append(bar_range)
        
        # Need enough history for moving average
        if len(self.closes) < self.ma_period:
            return
        
        # Compute moving average (trend filter)
        closes_list = list(self.closes)
        ma = sum(closes_list) / len(closes_list)
        current_price = closes_list[-1]
        
        # Need enough delta history to compute threshold
        if len(self.abs_deltas) < self.delta_lookback:
            return
        
        # Compute delta threshold from percentile
        abs_deltas_sorted = sorted(list(self.abs_deltas))
        threshold_idx = int(self.delta_percentile * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Current delta and its sign
        current_delta = delta
        
        # Compute volatility adjustment
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            avg_range = sum(ranges_list) / len(ranges_list)
            recent_range = ranges_list[-1]
            
            if avg_range > 0:
                vol_ratio = recent_range / avg_range
            else:
                vol_ratio = 1.0
        else:
            vol_ratio = 1.0
        
        # Clamp volatility ratio to reasonable bounds
        vol_ratio = max(0.6, min(vol_ratio, 1.5))
        
        # Adjust bracket levels based on volatility
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        # Entry logic: combine trend and order flow
        # Long: price above MA AND current delta is extremely positive
        if (current_price > ma and 
            current_delta > delta_threshold):
            
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-long",
            )
        
        # Short: price below MA AND current delta is extremely negative
        elif (current_price < ma and 
              current_delta < -delta_threshold):
            
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
