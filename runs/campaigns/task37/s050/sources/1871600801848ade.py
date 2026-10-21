"""
A volatility regime-based strategy with order flow filtering and dynamic thresholds.

Key insights from prior trials:
1. Trial #2 (score +2.4655) succeeded with trend-following + volatility adjustment
2. The mean reversion approaches (#0, #1) underperformed significantly
3. Trend-following with delta confirmation works better than pure mean reversion

This strategy improves on #2 by:
1. Adding regime detection: identify high/low volatility regimes
2. Tighter delta filtering: use percentile-based thresholds instead of fixed z-scores
3. Multi-bar confirmation: require trend alignment across multiple bars
4. Adaptive entry conditions: different thresholds for high vs low volatility regimes
5. Better position management: track entry conditions to avoid over-trading

The core improvement: instead of a fixed delta threshold, use percentile-based
filtering to adapt to the actual distribution of deltas. In high volatility,
extreme deltas are more common, so we need higher percentile thresholds. In
low volatility, even moderate deltas are significant.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        ma_period: int = 12,
        delta_lookback: int = 20,
        vol_lookback: int = 15,
        high_vol_threshold: float = 0.75,
        low_vol_delta_percentile: float = 0.75,
        high_vol_delta_percentile: float = 0.85,
        confirmation_bars: int = 2,
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
            high_vol_threshold: percentile threshold for high volatility regime
            low_vol_delta_percentile: delta percentile threshold in low vol
            high_vol_delta_percentile: delta percentile threshold in high vol
            confirmation_bars: bars that must align with trend
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.high_vol_threshold = high_vol_threshold
        self.low_vol_delta_percentile = low_vol_delta_percentile
        self.high_vol_delta_percentile = high_vol_delta_percentile
        self.confirmation_bars = confirmation_bars
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
        # Track closes for moving average
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track deltas for momentum
        self.deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track recent trend direction for confirmation
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
        bar_range = last.high - last.low
        self.ranges.append(bar_range)
        
        # Need enough history for moving average
        if len(self.closes) < self.ma_period:
            return
        
        # Compute moving average (trend filter)
        closes_list = list(self.closes)
        ma = sum(closes_list) / len(closes_list)
        current_price = closes_list[-1]
        
        # Determine trend direction
        price_above_ma = 1 if current_price > ma else (-1 if current_price < ma else 0)
        
        # Compute delta statistics
        if len(self.deltas) < self.delta_lookback:
            return
        
        deltas_list = list(self.deltas)
        deltas_sorted = sorted(deltas_list)
        
        # Compute volatility regime
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            vol_idx = int(self.high_vol_threshold * (len(ranges_sorted) - 1))
            vol_idx = max(0, min(vol_idx, len(ranges_sorted) - 1))
            recent_vol = ranges_sorted[vol_idx]
        else:
            recent_vol = bar_range if bar_range > 0 else 0.01
        
        avg_range = sum(self.ranges) / len(self.ranges) if self.ranges else 0.01
        vol_ratio = recent_vol / (avg_range + 1e-6)
        
        # Determine if we're in high or low volatility regime
        is_high_vol = vol_ratio > 1.0
        
        # Select delta threshold based on volatility regime
        if is_high_vol:
            delta_percentile = self.high_vol_delta_percentile
        else:
            delta_percentile = self.low_vol_delta_percentile
        
        # Get delta threshold from percentile
        delta_idx = int(delta_percentile * (len(deltas_sorted) - 1))
        delta_idx = max(0, min(delta_idx, len(deltas_sorted) - 1))
        delta_threshold = deltas_sorted[delta_idx]
        
        # Current delta
        current_delta = deltas_list[-1]
        
        # Determine signal: +1 for strong buy signal, -1 for strong sell signal, 0 for neutral
        signal = 0
        if current_delta > delta_threshold:
            signal = 1  # Strong buying
        elif current_delta < -delta_threshold:
            signal = -1  # Strong selling
        
        self.trend_signals.append(signal)
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            
            # Count positive and negative signals
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Long: price above MA, majority of signals positive, and current signal positive
            if (price_above_ma > 0 and positive_signals >= self.confirmation_bars - 1 
                and signal > 0):
                
                # Volatility-adjusted bracket levels
                stop_offset = self.base_stop_offset * max(0.6, min(vol_ratio, 1.4))
                target_offset = self.base_target_offset * max(0.6, min(vol_ratio, 1.4))
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="regime-long",
                )
            
            # Short: price below MA, majority of signals negative, and current signal negative
            elif (price_above_ma < 0 and negative_signals >= self.confirmation_bars - 1 
                  and signal < 0):
                
                # Volatility-adjusted bracket levels
                stop_offset = self.base_stop_offset * max(0.6, min(vol_ratio, 1.4))
                target_offset = self.base_target_offset * max(0.6, min(vol_ratio, 1.4))
                
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
