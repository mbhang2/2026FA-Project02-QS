"""
An order flow strategy with refined delta momentum and volatility-aware confirmation.

Key insights from prior trials:

1. Trial #36 (score +5.8422):
   - Soft delta momentum confirmation with 0.5x threshold
   - Adaptive delta threshold from 70th percentile
   - Combined volatility ratio scaling [0.6, 1.8]
   - Multi-bar confirmation with price direction alignment

2. Trial #37 (score +5.1594) - CURRENT BEST:
   - Adaptive momentum threshold based on delta volatility
   - High vol → 0.4x threshold (flexible), Low vol → 0.7x threshold (selective)
   - Market-adaptive filtering without hardcoding regimes
   - Maintains core formula from #26 and #36

3. Analysis of what works:
   - 70th percentile delta threshold is robust
   - Adaptive volatility scaling [0.6, 1.8] is well-calibrated
   - Multi-bar confirmation filters noise effectively
   - Momentum confirmation improves signal quality
   - Market adaptation (via delta_vol) is more effective than fixed thresholds

4. Observation from recent trials:
   - #36 (5.8422) > #37 (5.1594): fixed 0.5x momentum was better than adaptive
   - This suggests: the adaptive range [0.4, 0.7] may be too wide
   - Or: the momentum threshold optimization is less important than signal quality
   - The issue: #37 may over-filter in volatile periods or under-filter in calm periods

5. New hypothesis for improvement:
   - Return to fixed momentum threshold but optimize it further
   - #36 used 0.5x, which was strong at 5.8422
   - Test 0.45x threshold: slightly more selective than #36
   - This captures stronger momentum while filtering more noise
   - Simpler than adaptive approach, proven to work well
   - Also: tighten confirmation bar requirement from 2 to 3
   - This adds more signal alignment before entry
   - Reduces false positives without over-filtering
   - Combines: stronger momentum requirement + stricter confirmation

6. Alternative hypothesis:
   - Keep momentum at 0.5x (proven in #36)
   - Improve signal detection by refining delta threshold calculation
   - Instead of 70th percentile of absolute deltas, use 75th percentile
   - This makes the threshold slightly more selective
   - Captures only stronger order flow imbalances
   - Maintains momentum at proven 0.5x level
   - Reduces trade count slightly while improving quality

This strategy tests refined parameters from #36:
1. Keep all core logic from #36:
   - 75th percentile delta threshold (tighter than #26/#36's 70th)
   - Adaptive delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8]
   - Multi-bar confirmation with majority rule
   - MA period 10, delta lookback 14, vol lookback 12
   - Price direction confirmation

2. Refine momentum confirmation:
   - Keep fixed momentum threshold at 0.5x (proven in #36)
   - Tighten confirmation bars from 2 to 3
   - Require 3 recent bars to align with trend
   - This is stricter than #36 without changing momentum logic
   - Filters weak signals while maintaining trade count

3. Tighten delta threshold:
   - Use 75th percentile instead of 70th percentile
   - This makes signal detection more selective
   - Captures only strong order flow imbalances
   - Combined with stricter confirmation, should improve quality

Key hypothesis:
- #36 is strong at 5.8422
- Tighter threshold (75th percentile) + stricter confirmation (3 bars) improves selectivity
- Fixed 0.5x momentum is better than adaptive approach
- Should improve on #36 by reducing false positives
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
        confirmation_bars: int = 3,
        momentum_threshold: float = 0.5,
        delta_percentile: float = 0.75,
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
            delta_percentile: percentile for delta threshold (0.75 = 75th)
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
        self.delta_percentile = delta_percentile
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
        
        # Compute delta threshold from percentile of absolute deltas
        # Using 75th percentile (tighter than #26/#36's 70th)
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        threshold_idx = int(self.delta_percentile * (len(abs_deltas_sorted) - 1))
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
        # Stricter requirement: confirmation_bars = 3 instead of 2
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
            # Stricter: require all confirmation_bars signals to be positive
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars and 
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
            # Stricter: require all confirmation_bars signals to be negative
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars and 
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
