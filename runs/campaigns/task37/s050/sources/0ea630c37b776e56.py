"""
A trend-following strategy with momentum confirmation and volatility-adjusted position sizing.

Building on previous mean-reversion approaches, this strategy pivots to trend-following:
1. Trend identification: use price momentum (close vs. moving average)
2. Momentum confirmation: require delta to align with price trend
3. Volatility-adjusted sizing: scale position based on recent volatility
4. Dynamic exits: use volatility-adjusted bracket levels
5. Session reset: clear state at session boundaries

The insight: mean reversion has underperformed in prior trials (-0.70 to -0.73).
Trend-following may work better: buy when price is above MA and delta is positive
(institutional buying), sell when price is below MA and delta is negative.

This avoids tick subscription (5x cost penalty) while using bar-level order flow
signals (buy_volume, sell_volume). Volatility adjustment ensures we don't over-trade
in choppy conditions and don't under-trade when trends are strong.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        ma_period: int = 10,
        delta_lookback: int = 5,
        vol_lookback: int = 10,
        delta_threshold: float = 0.3,
        vol_percentile: float = 0.4,
        size: int = 1,
        base_stop_offset: float = 0.75,
        base_target_offset: float = 0.75,
    ):
        """
        Args:
            interval: bar interval to subscribe to
            ma_period: period for moving average (trend filter)
            delta_lookback: bars to accumulate delta over
            vol_lookback: bars for volatility measurement
            delta_threshold: z-score threshold for delta extremeness
            vol_percentile: volatility percentile for scaling
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.delta_threshold = delta_threshold
        self.vol_percentile = vol_percentile
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
        # Track closes for moving average
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track deltas for momentum
        self.deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
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
        
        # Compute delta statistics
        if len(self.deltas) < self.delta_lookback:
            return
        
        deltas_list = list(self.deltas)
        mean_delta = sum(deltas_list) / len(deltas_list)
        variance = sum((d - mean_delta) ** 2 for d in deltas_list) / len(deltas_list)
        std_delta = variance ** 0.5
        
        if std_delta <= 0:
            return
        
        # Normalize current delta
        current_delta = deltas_list[-1]
        z_score = (current_delta - mean_delta) / std_delta
        
        # Compute volatility
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            vol_idx = int(self.vol_percentile * (len(ranges_sorted) - 1))
            vol_idx = max(0, min(vol_idx, len(ranges_sorted) - 1))
            recent_vol = ranges_sorted[vol_idx]
        else:
            recent_vol = bar_range if bar_range > 0 else 0.01
        
        avg_range = sum(self.ranges) / len(self.ranges) if self.ranges else 0.01
        vol_ratio = recent_vol / (avg_range + 1e-6)
        
        # Volatility-adjusted bracket levels
        # Higher volatility -> wider brackets; lower volatility -> tighter brackets
        stop_offset = self.base_stop_offset * max(0.7, min(vol_ratio, 1.5))
        target_offset = self.base_target_offset * max(0.7, min(vol_ratio, 1.5))
        
        # Trend-following with momentum confirmation
        # Long: price above MA AND positive delta (institutional buying)
        if current_price > ma and z_score > self.delta_threshold:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Short: price below MA AND negative delta (institutional selling)
        elif current_price < ma and z_score < -self.delta_threshold:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
