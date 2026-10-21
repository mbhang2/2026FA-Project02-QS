"""
An order flow strategy with refined delta signal detection and adaptive entry validation.

Key insights from prior trials:

1. Trial #26 (score +5.7681) - BEST:
   - Adaptive delta threshold based on delta volatility
   - Combined volatility ratio scaling (price vol * delta vol)
   - Multi-bar confirmation with price direction alignment
   - 70th percentile delta threshold proven optimal
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #29 (score +2.9879):
   - Tightened confirmation to require ALL signals aligned
   - Improved delta_vol_ratio calibration [0.65, 1.4]
   - Better vol ratio bounds [0.55, 1.9]
   - Underperformed #26, suggesting stricter confirmation over-filters

3. Analysis of what makes #26 optimal:
   - The adaptive threshold scaling is the key innovation
   - Combined volatility ratio for brackets is well-calibrated
   - Multi-bar confirmation with price direction (not ALL aligned) is the sweet spot
   - Majority-based confirmation (not strict ALL) captures more valid signals
   - The core #26 formula is near-optimal

4. Key observation from #29's underperformance:
   - Requiring ALL signals aligned is too strict
   - #26's majority-based approach (>= confirmation_bars - 1) is better
   - The issue is not confirmation strictness but signal quality

This strategy refines #26 by:
1. Keeping all proven core logic from #26 exactly:
   - Adaptive delta threshold with delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8] for bracket scaling
   - Multi-bar confirmation with price direction checks (majority-based)
   - MA period 10, delta lookback 14, vol lookback 12

2. Adding refined entry validation:
   - Check that the current bar's delta is not just extreme but DIRECTIONALLY CONSISTENT
   - Measure recent delta trend (last 3 bars) to ensure signal is not reversing
   - Only trade when delta extremeness is sustained or growing
   - This filters false signals that occur at turning points

3. Adding volatility regime awareness:
   - Measure the ratio of current delta volatility to historical average
   - In high-volatility regimes, be more selective (require higher threshold percentile)
   - In low-volatility regimes, be more aggressive (allow lower threshold percentile)
   - This adapts to market conditions without over-filtering

4. Refining price confirmation:
   - Ensure price momentum matches delta signal direction
   - Check that recent closes are moving in the trade direction
   - This adds a secondary validation without being too strict

5. Keeping all parameters that made #26 work:
   - MA period 10 (trend filter)
   - Delta lookback 14 (distribution measurement)
   - Vol lookback 12 (price volatility)
   - Confirmation bars 2 (multi-bar alignment with majority rule)
   - Adaptive threshold scaling from #26
   - Combined volatility ratio scaling from #26

Key hypothesis:
- #26 works because it balances all factors well
- We can improve by: adding delta trend consistency check
- Recent delta trend (are we accelerating or decelerating?) indicates signal quality
- High-volatility regime detection improves signal selection
- Price momentum alignment (not just direction) strengthens confirmation
- The combination of: adaptive threshold + delta trend + regime awareness + price momentum
  should improve on #26's score of 5.7681 without the over-filtering of #29

The strategy:
1. Tracks price trend via moving average (MA period 10)
2. Measures delta volatility for adaptive threshold scaling
3. Measures delta trend consistency (recent deltas moving in signal direction)
4. Measures volatility regime (current vol vs historical average)
5. Scales delta threshold adaptively based on both delta volatility and regime
6. Uses multi-bar confirmation with majority rule (like #26, not strict like #29)
7. Filters by delta trend consistency and price momentum alignment
8. Scales both stop and target based on combined volatility ratio
9. Combines: MA trend + adaptive threshold + delta trend + regime awareness
"""

from __future__ import annotations
from collections import deque
import math


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
        delta_trend_lookback: int = 3,
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
            delta_trend_lookback: bars to check for delta trend consistency
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.confirmation_bars = confirmation_bars
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        self.delta_trend_lookback = delta_trend_lookback
        
        # Track closes for moving average
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track deltas for distribution analysis
        self.deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track bar ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track trend signals for multi-bar confirmation
        self.trend_signals: deque[int] = deque(maxlen=confirmation_bars)
        
        # Track recent price direction for secondary trend confirmation
        self.price_directions: deque[int] = deque(maxlen=3)
        
        # Track recent deltas for trend consistency check
        self.recent_deltas: deque[float] = deque(maxlen=delta_trend_lookback)
        
        # Track historical delta volatility for regime detection
        self.delta_vols: deque[float] = deque(maxlen=10)
        
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _compute_delta_volatility(self, deltas_list: list[float]) -> float:
        """Compute the standard deviation of deltas."""
        if len(deltas_list) < 2:
            return 1.0
        
        mean_delta = sum(deltas_list) / len(deltas_list)
        variance = sum((d - mean_delta) ** 2 for d in deltas_list) / len(deltas_list)
        
        if variance <= 0:
            return 1.0
        
        return math.sqrt(variance)

    def _check_delta_trend_consistency(self, recent_deltas_list: list[float], signal: int) -> bool:
        """
        Check if recent delta trend is consistent with the signal direction.
        For a buy signal, recent deltas should show positive trend.
        For a sell signal, recent deltas should show negative trend.
        """
        if len(recent_deltas_list) < 2:
            return True  # Not enough data, allow the trade
        
        # Check if deltas are moving in the signal direction
        if signal > 0:
            # Buy signal: expect recent deltas to be positive or increasing
            positive_count = sum(1 for d in recent_deltas_list if d > 0)
            return positive_count >= len(recent_deltas_list) - 1
        elif signal < 0:
            # Sell signal: expect recent deltas to be negative or decreasing
            negative_count = sum(1 for d in recent_deltas_list if d < 0)
            return negative_count >= len(recent_deltas_list) - 1
        
        return True

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.trend_signals.clear()
            self.price_directions.clear()
            self.recent_deltas.clear()
            self.delta_vols.clear()
        
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
        self.recent_deltas.append(delta)
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
        
        # Compute delta volatility for adaptive thresholding
        deltas_list = list(self.deltas)
        delta_vol = self._compute_delta_volatility(deltas_list)
        self.delta_vols.append(delta_vol)
        
        # Compute delta threshold from percentile of absolute deltas
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # Adapt percentile threshold based on volatility regime
        # High volatility regime: use higher percentile (70th) for stricter filtering
        # Low volatility regime: use lower percentile (65th) for more signals
        if len(self.delta_vols) >= 3:
            avg_vol = sum(self.delta_vols) / len(self.delta_vols)
            vol_ratio = delta_vol / avg_vol if avg_vol > 0 else 1.0
            
            # High volatility (vol_ratio > 1.2): use 72nd percentile
            # Normal volatility (0.8-1.2): use 70th percentile
            # Low volatility (< 0.8): use 68th percentile
            if vol_ratio > 1.2:
                percentile = 0.72
            elif vol_ratio < 0.8:
                percentile = 0.68
            else:
                percentile = 0.70
        else:
            percentile = 0.70
        
        threshold_idx = int(percentile * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Adapt threshold based on delta volatility (from #26)
        delta_vol_ratio = max(0.7, min(delta_vol / 50.0, 1.3))
        adapted_threshold = delta_threshold * delta_vol_ratio
        
        # Current delta
        current_delta = delta
        
        # Determine signal based on delta extremeness
        signal = 0
        if current_delta > adapted_threshold:
            signal = 1  # Strong buy signal
        elif current_delta < -adapted_threshold:
            signal = -1  # Strong sell signal
        
        self.trend_signals.append(signal)
        
        # Check delta trend consistency
        recent_deltas_list = list(self.recent_deltas)
        delta_trend_consistent = self._check_delta_trend_consistency(recent_deltas_list, signal)
        
        # Compute volatility adjustment using 65th percentile (from #26)
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            vol_idx = int(0.65 * (len(ranges_sorted) - 1))
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
        vol_ratio = max(0.6, min(vol_ratio, 1.6))
        
        # Scale bracket levels based on both price volatility and delta volatility (from #26)
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.6, min(combined_vol_ratio, 1.8))
        
        stop_offset = self.base_stop_offset * combined_vol_ratio
        target_offset = self.base_target_offset * combined_vol_ratio
        
        # Multi-bar confirmation: check if recent signals align with trend (like #26, not strict like #29)
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # delta trend is consistent, and recent price action shows upward bias
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                delta_trend_consistent and
                positive_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-confirmed-long",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # delta trend is consistent, and recent price action shows downward bias
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  delta_trend_consistent and
                  negative_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-confirmed-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
