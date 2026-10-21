"""
An order flow strategy with refined signal detection and momentum confirmation.

Key insights from prior trials:

1. Trial #36 (score +5.8422) - BEST OVERALL:
   - Soft delta momentum confirmation with 0.5x threshold
   - Adaptive delta threshold from 70th percentile
   - Combined volatility ratio scaling [0.6, 1.8]
   - Multi-bar confirmation with price direction alignment
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #39 (score +1.1350):
   - Added momentum acceleration check (current > previous)
   - Combined with base momentum (current > 0.5 * previous)
   - Score dropped significantly: dual-check was too restrictive
   - Reduced trade count without improving quality

3. Trial #37 (score +5.1594):
   - Adaptive momentum threshold based on delta volatility
   - Adaptive range [0.4, 0.7] was too wide
   - Over-filtered in volatile periods, under-filtered in calm periods

4. Analysis of what works:
   - #36's fixed 0.5x momentum threshold is optimal
   - 70th percentile delta threshold is well-calibrated
   - 2 confirmation bars is the sweet spot
   - Combined vol ratio [0.6, 1.8] is robust
   - Adding extra conditions (acceleration, tighter percentiles) reduces score
   - The core formula is near-optimal; over-engineering degrades it

5. Observation from recent trials:
   - #36 at 5.8422 is the best we've achieved
   - Attempts to refine confirmation (#38, #39) consistently fail
   - The issue: we're trying to filter signals that are already good
   - Instead of filtering more, we should IMPROVE SIGNAL QUALITY at source
   - The opportunity is in VOLATILITY-ADJUSTED DELTA THRESHOLD calculation

6. New hypothesis for improvement over #36:
   - #36 is strong at 5.8422 - keep the core momentum and confirmation logic
   - The opportunity is in REFINED DELTA THRESHOLD CALCULATION
   - Current approach: 70th percentile of absolute deltas, then multiply by delta_vol_ratio
   - Problem: delta_vol_ratio [0.7, 1.3] may not be optimal across all volatility regimes
   - New approach: use a PERCENTILE-ADJUSTED threshold that varies with delta volatility
   - In high delta volatility: use higher percentile (75th) to be more selective
   - In low delta volatility: use lower percentile (65th) to be more responsive
   - This adapts the threshold CALCULATION, not just the result
   - More elegant than fixed percentile + fixed vol ratio
   - Captures market regime changes in delta behavior

7. Percentile adaptation approach:
   - Compute delta volatility as before
   - Normalize delta_vol to [0, 1] range for percentile calculation
   - Use adaptive percentile: base_percentile + vol_adjusted_delta
   - High vol (delta_vol > 50): use 75th percentile (more selective)
   - Low vol (delta_vol < 30): use 65th percentile (more responsive)
   - Medium vol: interpolate between 65th and 75th
   - This makes threshold responsive to market regime
   - Keeps momentum and confirmation logic unchanged

This strategy refines #36 by:
1. Keeping all proven logic from #36:
   - Adaptive delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8]
   - Multi-bar confirmation with majority rule
   - MA period 10, delta lookback 14, vol lookback 12
   - Price direction confirmation
   - Fixed 0.5x momentum threshold

2. Refining delta threshold calculation:
   - Use adaptive percentile based on delta volatility
   - Low vol (delta_vol < 30): 65th percentile
   - High vol (delta_vol > 50): 75th percentile
   - Medium vol: linear interpolation between 65th and 75th
   - This makes signal detection regime-aware
   - More selective in volatile periods, more responsive in calm periods

3. Maintaining all proven parameters:
   - MA period 10, delta lookback 14, vol lookback 12
   - Confirmation bars 2 with majority rule
   - Base stop/target offset 0.75
   - Combined vol ratio [0.6, 1.8]
   - Fixed 0.5x momentum threshold

Key hypothesis:
- #36 is strong at 5.8422
- Adaptive percentile threshold improves signal quality across regimes
- Not adding new filters, but improving the core signal calculation
- Should improve on #36 by adapting to market volatility regimes
- Maintains simplicity and interpretability
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
        momentum_threshold: float = 0.5,
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
            momentum_threshold: multiplier for momentum check
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.confirmation_bars = confirmation_bars
        self.momentum_threshold = momentum_threshold
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
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
        
        # Track previous delta for momentum confirmation
        self.prev_delta: float = 0.0
        
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

    def _get_adaptive_percentile(self, delta_vol: float) -> float:
        """
        Compute adaptive percentile based on delta volatility.
        Low vol (delta_vol < 30): 65th percentile (responsive)
        High vol (delta_vol > 50): 75th percentile (selective)
        Medium vol: interpolate between 65th and 75th
        """
        if delta_vol < 30:
            return 0.65
        elif delta_vol > 50:
            return 0.75
        else:
            # Linear interpolation between 65th and 75th
            # At vol=30, percentile=0.65; at vol=50, percentile=0.75
            ratio = (delta_vol - 30) / (50 - 30)
            return 0.65 + ratio * (0.75 - 0.65)

    def _check_momentum(self, current_delta: float, signal: int) -> bool:
        """
        Check if delta momentum aligns with signal direction.
        Fixed momentum threshold at 0.5x (proven in #36).
        """
        if signal == 0:
            return False
        
        if signal > 0:
            # Buy signal: require current_delta > 0.5 * previous_delta
            return current_delta > self.momentum_threshold * self.prev_delta
        else:
            # Sell signal: require current_delta < 0.5 * previous_delta
            return current_delta < self.momentum_threshold * self.prev_delta

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.trend_signals.clear()
            self.price_directions.clear()
            self.prev_delta = 0.0
        
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
            self.prev_delta = delta
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
            self.prev_delta = delta
            return
        
        # Compute delta volatility for adaptive thresholding
        deltas_list = list(self.deltas)
        delta_vol = self._compute_delta_volatility(deltas_list)
        
        # Get adaptive percentile based on delta volatility
        adaptive_percentile = self._get_adaptive_percentile(delta_vol)
        
        # Compute delta threshold from adaptive percentile of absolute deltas
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        threshold_idx = int(adaptive_percentile * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Adapt threshold based on delta volatility
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
        
        # Compute volatility adjustment using 65th percentile
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
        
        # Scale bracket levels based on both price volatility and delta volatility
        combined_vol_ratio = vol_ratio * delta_vol_ratio
        combined_vol_ratio = max(0.6, min(combined_vol_ratio, 1.8))
        
        stop_offset = self.base_stop_offset * combined_vol_ratio
        target_offset = self.base_target_offset * combined_vol_ratio
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Check momentum confirmation
            momentum_ok = self._check_momentum(current_delta, signal)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # recent price action shows upward bias, AND momentum confirms
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                positive_price_dirs >= len(price_dirs_list) - 1 and
                momentum_ok):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # recent price action shows downward bias, AND momentum confirms
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  negative_price_dirs >= len(price_dirs_list) - 1 and
                  momentum_ok):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
        
        # Update previous delta for next bar
        self.prev_delta = current_delta

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
