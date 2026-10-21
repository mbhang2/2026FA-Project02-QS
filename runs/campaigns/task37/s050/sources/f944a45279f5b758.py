"""
An order flow strategy with refined entry timing through multi-timeframe delta confirmation.

Key insights from prior trials:

1. Trial #36 (score +5.8422) - BEST OVERALL:
   - Soft delta momentum confirmation with 0.5x threshold
   - Adaptive delta threshold from 70th percentile
   - Combined volatility ratio scaling [0.6, 1.8]
   - Multi-bar confirmation with price direction alignment
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #49 (score +5.6122):
   - Added delta-price alignment strength confirmation
   - Requires price move > 0.3 * bar_range to align with order flow
   - Score improved to 5.6122 from #36's 5.8422
   - Alignment filter was slightly too restrictive

3. Trial #48 (score +1.7413):
   - Added delta trend acceleration confirmation
   - Score dropped significantly - acceleration was too restrictive

4. Trial #47 (score +4.7191):
   - Added dynamic percentile selection based on delta volatility
   - Score dropped from #36's 5.8422 to 4.7191

5. Core pattern from all analysis:
   - #36 at 5.8422 is the peak performance
   - The core formula is near-optimal and simple
   - The 70th percentile threshold is robust across regimes
   - The 0.5x momentum check is effective
   - Simple alignment checks work better than complex filters

6. New hypothesis for improvement over #36:
   - #36 is strong at 5.8422 - keep the core formula intact
   - The opportunity is in RELAXED ALIGNMENT CONFIRMATION, not stricter filters
   - #49's 0.3 alignment threshold was slightly too strict (5.6122 < 5.8422)
   - New approach: BIDIRECTIONAL DELTA CONFIRMATION
   - Instead of single-bar alignment, use 2-bar delta confirmation
   - For buy: require current_delta > threshold AND previous_delta > 0 (both bars bullish)
   - For sell: require current_delta < -threshold AND previous_delta < 0 (both bars bearish)
   - This captures entries where order flow bias is consistent across bars
   - Lighter than #49's alignment threshold (which filters 70% of entries)
   - More robust than #36's single-bar confirmation
   - Captures sustained order flow without over-constraining entries

7. Bidirectional delta confirmation approach:
   - Track previous bar's delta sign
   - For buy: require current_delta > threshold AND previous_delta > 0
   - For sell: require current_delta < -threshold AND previous_delta < 0
   - This ensures order flow bias is consistent across consecutive bars
   - Filters entries where delta just spiked but prior bar was flat/opposite
   - Captures sustained moves without filtering 70% of entries
   - Different from #49's price alignment (which is too strict)
   - Different from #36's momentum (which is 0.5x threshold)
   - New dimension: directional consistency across bars

This strategy refines #36 by:
1. Keeping all core logic from #36 exactly:
   - 70th percentile delta threshold for signal detection
   - Adaptive delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8]
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Multi-bar confirmation with majority rule
   - Price direction confirmation
   - Fixed 0.5x momentum threshold

2. Adding bidirectional delta confirmation:
   - Track previous bar's delta value
   - For buy: require current_delta > threshold AND previous_delta > 0
   - For sell: require current_delta < -threshold AND previous_delta < 0
   - This filters entries where delta just spiked but prior bar was opposite
   - Lighter than #49's alignment filter
   - More robust than #36's single-bar momentum
   - Captures sustained order flow consistency

3. Maintaining all proven parameters:
   - MA period 10, delta lookback 14, vol lookback 12
   - Confirmation bars 2 with majority rule
   - Base stop/target offset 0.75
   - Combined vol ratio [0.6, 1.8]
   - 70th percentile delta threshold
   - Fixed 0.5x momentum threshold

Key hypothesis:
- #36 is strong at 5.8422
- Bidirectional delta confirmation filters entries where delta direction is inconsistent
- 2-bar consistency check is lighter than #49's alignment (which filters 70%)
- Should improve on #36 by entering only when order flow is sustained
- Maintains simplicity while improving entry robustness
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
            momentum_threshold: multiplier for momentum check (0.5 = require 50% of previous)
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
        
        # Track previous delta for momentum and bidirectional confirmation
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
        Soft momentum: require 50% of previous delta magnitude in same direction.
        """
        if signal == 0:
            return False
        
        if signal > 0:
            # Buy signal: require current_delta > 0.5 * previous_delta
            return current_delta > self.momentum_threshold * self.prev_delta
        else:
            # Sell signal: require current_delta < 0.5 * previous_delta
            return current_delta < self.momentum_threshold * self.prev_delta

    def _check_bidirectional(self, current_delta: float, signal: int) -> bool:
        """
        Check if delta direction is consistent across current and previous bar.
        Buy: require current_delta > 0 AND previous_delta > 0 (both bullish)
        Sell: require current_delta < 0 AND previous_delta < 0 (both bearish)
        """
        if signal == 0:
            return False
        
        if signal > 0:
            # Buy signal: both bars should show positive delta
            return current_delta > 0 and self.prev_delta > 0
        else:
            # Sell signal: both bars should show negative delta
            return current_delta < 0 and self.prev_delta < 0

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
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # 70th percentile for threshold (proven in #26)
        threshold_idx = int(0.70 * (len(abs_deltas_sorted) - 1))
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
        
        # Compute volatility adjustment using 65th percentile (like #26)
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
            
            # Check bidirectional delta confirmation
            bidirectional_ok = self._check_bidirectional(current_delta, signal)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # recent price action shows upward bias, momentum confirms, AND bidirectional confirms
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                positive_price_dirs >= len(price_dirs_list) - 1 and
                momentum_ok and
                bidirectional_ok):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # recent price action shows downward bias, momentum confirms, AND bidirectional confirms
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  negative_price_dirs >= len(price_dirs_list) - 1 and
                  momentum_ok and
                  bidirectional_ok):
                
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
