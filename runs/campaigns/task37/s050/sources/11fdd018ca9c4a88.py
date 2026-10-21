"""
A volatility-adjusted mean reversion strategy with adaptive thresholds.

Building on the previous mean reversion approach, this strategy adds:
1. Volatility measurement: track price range volatility to scale thresholds
2. Adaptive delta threshold: adjust sensitivity based on recent volatility
3. Multi-bar confirmation: require delta extremeness over multiple bars
4. Session-aware resets: clear state at session boundaries
5. Position sizing: scale position based on measured volatility

The core insight: mean reversion works better when volatility is measured
and thresholds are adaptive. Extreme deltas in high-volatility regimes may
be normal; in low-volatility regimes they're more significant.

Strategy:
- Track price range (high-low) to measure volatility
- Compute rolling volatility from recent bars
- Adjust delta threshold based on volatility regime
- Require delta extremeness to persist for confirmation
- Only trade when conditions align (extreme delta + low volatility = high confidence)
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 20,
        vol_lookback: int = 10,
        base_delta_threshold: float = 0.5,
        vol_percentile: float = 0.3,
        confirmation_bars: int = 1,
        size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
    ):
        """
        Args:
            interval: bar interval to subscribe to
            lookback: number of bars for delta statistics
            vol_lookback: number of bars for volatility measurement
            base_delta_threshold: base z-score threshold for delta extremeness
            vol_percentile: volatility percentile (0-1) to use for scaling
            confirmation_bars: bars that must show extreme delta
            size: contract size
            stop_offset: protective stop distance
            target_offset: profit target distance
        """
        self.interval = interval
        self.lookback = lookback
        self.vol_lookback = vol_lookback
        self.base_delta_threshold = base_delta_threshold
        self.vol_percentile = vol_percentile
        self.confirmation_bars = confirmation_bars
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        # Accumulate deltas and ranges
        self.deltas: deque[float] = deque(maxlen=lookback)
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        self.z_scores: deque[float] = deque(maxlen=confirmation_bars)
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset accumulation on session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.deltas.clear()
            self.ranges.clear()
            self.z_scores.clear()
        
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last = history[-1]
        
        # Accumulate delta and range from the last bar
        delta = float(last.buy_volume - last.sell_volume)
        self.deltas.append(delta)
        
        bar_range = last.high - last.low
        self.ranges.append(bar_range)
        
        # Need enough history for delta statistics
        if len(self.deltas) < self.lookback:
            return
        
        # Compute delta statistics
        deltas_list = list(self.deltas)
        mean_delta = sum(deltas_list) / len(deltas_list)
        variance = sum((d - mean_delta) ** 2 for d in deltas_list) / len(deltas_list)
        std_delta = variance ** 0.5
        
        if std_delta <= 0:
            return
        
        # Compute volatility from ranges
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            vol_idx = int(self.vol_percentile * (len(ranges_sorted) - 1))
            vol_idx = max(0, min(vol_idx, len(ranges_sorted) - 1))
            recent_vol = ranges_sorted[vol_idx]
        else:
            recent_vol = last.high - last.low if last.high > last.low else 0.01
        
        # Adaptive threshold: scale by volatility
        # Low volatility -> lower threshold (more sensitive)
        # High volatility -> higher threshold (less sensitive)
        avg_range = sum(self.ranges) / len(self.ranges) if self.ranges else 0.01
        vol_ratio = recent_vol / (avg_range + 1e-6)
        adjusted_threshold = self.base_delta_threshold * max(0.5, vol_ratio)
        
        # Normalize current delta
        current_delta = deltas_list[-1]
        z_score = (current_delta - mean_delta) / std_delta
        self.z_scores.append(z_score)
        
        # Confirmation: check if multiple bars show extremeness in same direction
        if len(self.z_scores) >= self.confirmation_bars:
            z_scores_list = list(self.z_scores)
            all_positive = all(z > adjusted_threshold for z in z_scores_list)
            all_negative = all(z < -adjusted_threshold for z in z_scores_list)
            
            if all_positive:
                # Extreme buying persists; expect mean reversion down
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="mr-short",
                )
            elif all_negative:
                # Extreme selling persists; expect mean reversion up
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="mr-long",
                )
        elif len(self.z_scores) == 1:
            # First bar of confirmation window; single bar extremeness (less strict)
            if z_score > adjusted_threshold * 0.8:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="mr-short",
                )
            elif z_score < -adjusted_threshold * 0.8:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="mr-long",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
