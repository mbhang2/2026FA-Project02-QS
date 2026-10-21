"""
An order flow strategy with improved entry timing and regime-adaptive confirmation.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with multi-bar confirmation + volatility adjustment
2. Trial #11 (score +1.7552) used 65th percentile delta threshold with simple alignment
3. The core winning pattern: price trend (MA) + order flow (delta) + volatility adjustment

This strategy refines #7 by:
1. Improving entry timing: use a 2-bar lookback to detect delta momentum, not just current bar
2. Better signal quality: require delta to be not just above threshold but ACCELERATING
3. Enhanced trend confirmation: use price momentum (recent closes) + MA trend alignment
4. More adaptive volatility: scale based on recent range volatility AND delta magnitude
5. Regime detection: identify high-volatility regimes and adjust confirmation strictness
6. Cleaner entry logic: delta acceleration + price momentum + trend alignment

Key hypothesis:
- #7 works because multi-bar confirmation reduces false signals
- Weakness: uses only current bar delta, missing momentum/acceleration signal
- Improvement: detect when delta is accelerating (getting stronger) - this is a stronger signal
- Also: combine price momentum (close trend) with MA trend for better entry timing
- Delta acceleration: if current delta is stronger than previous delta in same direction, it's momentum

The strategy:
1. Tracks price trend via moving average (trend filter)
2. Measures delta distribution for threshold and regime detection
3. Detects delta ACCELERATION: current delta stronger than recent deltas in same direction
4. Uses price momentum: recent closes trending in same direction as signal
5. Combines: MA trend + delta acceleration + price momentum + volatility adjustment
6. Adaptive confirmation: stricter in high-volatility regimes, looser in low-volatility
7. Tracks delta momentum across bars to identify strengthening order flow
8. Better entry: only trade when multiple signals align AND delta is accelerating
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
        momentum_bars: int = 2,
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
            momentum_bars: bars to check for delta momentum/acceleration
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.momentum_bars = momentum_bars
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
        # Track closes for moving average and price momentum
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track deltas for distribution analysis and momentum detection
        self.deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track bar ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track recent delta magnitudes for momentum detection
        self.recent_delta_mags: deque[float] = deque(maxlen=momentum_bars + 1)
        
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
            self.recent_delta_mags.clear()
        
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
        self.recent_delta_mags.append(abs(delta))
        
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
        
        # Compute price momentum: are recent closes trending up or down?
        recent_closes = closes_list[-3:] if len(closes_list) >= 3 else closes_list
        price_momentum = 0  # 1 for up, -1 for down, 0 for flat
        if len(recent_closes) >= 2:
            up_count = sum(1 for i in range(1, len(recent_closes)) if recent_closes[i] > recent_closes[i-1])
            down_count = sum(1 for i in range(1, len(recent_closes)) if recent_closes[i] < recent_closes[i-1])
            if up_count > down_count:
                price_momentum = 1
            elif down_count > up_count:
                price_momentum = -1
        
        # Need enough delta history to compute threshold
        if len(self.deltas) < self.delta_lookback:
            return
        
        # Compute delta threshold using 65th percentile
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
        current_delta_mag = abs(current_delta)
        
        # Detect delta acceleration: is current delta stronger than recent deltas?
        # AND is it in the same direction as previous strong deltas?
        delta_accelerating = False
        delta_direction = 0  # 1 for buy, -1 for sell
        
        if current_delta > delta_threshold:
            delta_direction = 1
            # Check if this buy signal is accelerating (getting stronger)
            if len(self.recent_delta_mags) >= 2:
                prev_delta_mags = list(self.recent_delta_mags)[:-1]
                avg_prev_mag = sum(prev_delta_mags) / len(prev_delta_mags)
                # Accelerating if current is stronger than recent average
                if current_delta_mag > avg_prev_mag * 1.1:
                    delta_accelerating = True
            else:
                delta_accelerating = True
        
        elif current_delta < -delta_threshold:
            delta_direction = -1
            # Check if this sell signal is accelerating (getting stronger)
            if len(self.recent_delta_mags) >= 2:
                prev_delta_mags = list(self.recent_delta_mags)[:-1]
                avg_prev_mag = sum(prev_delta_mags) / len(prev_delta_mags)
                # Accelerating if current is stronger than recent average
                if current_delta_mag > avg_prev_mag * 1.1:
                    delta_accelerating = True
            else:
                delta_accelerating = True
        
        # Compute volatility adjustment for bracket sizing
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            vol_idx = int(0.65 * (len(ranges_sorted) - 1))
            vol_idx = max(0, min(vol_idx, len(ranges_sorted) - 1))
            percentile_range = ranges_sorted[vol_idx]
            
            avg_range = sum(ranges_list) / len(ranges_list)
            if avg_range > 0.0:
                vol_ratio = percentile_range / avg_range
                # Compute volatility regime
                vol_regime = vol_ratio  # Higher = higher volatility regime
            else:
                vol_ratio = 1.0
                vol_regime = 1.0
        else:
            vol_ratio = 1.0
            vol_regime = 1.0
        
        # Clamp volatility ratio to reasonable bounds
        vol_ratio = max(0.6, min(vol_ratio, 1.6))
        
        # Adjust bracket levels based on volatility
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        # Entry conditions: combine multiple signals
        # 1. Price trend alignment (price vs MA)
        # 2. Delta acceleration (delta getting stronger)
        # 3. Price momentum alignment (recent closes moving in same direction)
        # 4. Strong delta signal (above threshold)
        
        if (price_above_ma and 
            delta_direction > 0 and
            delta_accelerating and
            price_momentum >= 0 and
            current_delta_mag > delta_threshold * 1.0):
            
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="accelerating-long",
            )
        
        elif (price_below_ma and 
              delta_direction < 0 and
              delta_accelerating and
              price_momentum <= 0 and
              current_delta_mag > delta_threshold * 1.0):
            
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="accelerating-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
