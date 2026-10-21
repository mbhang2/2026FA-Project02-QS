"""
An order flow strategy with refined multi-timeframe delta confirmation.

Key insights from prior trials:

1. Trial #36 (score +5.8422) - BEST OVERALL:
   - Soft delta momentum confirmation with 0.5x threshold
   - Adaptive delta threshold from 70th percentile
   - Combined volatility ratio scaling [0.6, 1.8]
   - Multi-bar confirmation with price direction alignment
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #53 (score +5.7679):
   - Volatility-regime-based momentum (0.6/0.45/0.35 thresholds)
   - Close to #36 but slightly below
   - Regime adaptation added complexity without improving score

3. Trial #51 (score +5.8128):
   - Signal strength-weighted momentum (0.2x for strong, 0.35x for normal, 0.5x for weak)
   - Very close to #36 at 5.8128 vs 5.8422
   - Only 0.0294 behind - the adaptive approach was nearly optimal

4. Core pattern analysis:
   - #36 at 5.8422 is the peak - the 0.5x fixed momentum is optimal
   - Attempts to improve with regime adaptation (#53) or acceleration (#52) degrade results
   - The 70th percentile threshold is robust
   - Multi-bar confirmation with price direction is critical
   - Signal strength weighting in #51 was close but slightly too aggressive

5. New hypothesis for improvement over #36:
   - #36 is strong at 5.8422 with 0.5x fixed momentum
   - The opportunity is in DELTA MEAN REVERSION confirmation
   - Current approach: require current_delta > 0.5 * previous_delta (momentum)
   - Problem: this enters on continuation, which can be late in moves
   - New approach: enter when delta is EXTREME relative to recent mean
   - For buy: require current_delta > mean(recent_deltas) + 0.5 * std(recent_deltas)
   - For sell: require current_delta < mean(recent_deltas) - 0.5 * std(recent_deltas)
   - This captures turning points where order flow reaches extremes
   - More robust than momentum, which can fail in choppy markets
   - Lighter than multi-bar confirmation, which can miss quick reversals

6. Mean reversion approach:
   - Track 5-bar rolling mean and std of deltas
   - For buy: require current_delta > rolling_mean + 0.5 * rolling_std
   - For sell: require current_delta < rolling_mean - 0.5 * rolling_std
   - This captures extremes that are likely to revert
   - Combines with 70th percentile threshold for double confirmation
   - Should improve on #36 by entering at turning points rather than continuations

This strategy refines #36 by:
1. Keeping all core logic from #36 exactly:
   - 70th percentile delta threshold for signal detection
   - Adaptive delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8]
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Multi-bar confirmation with majority rule
   - Price direction confirmation

2. Replacing fixed momentum with mean reversion confirmation:
   - Compute 5-bar rolling mean of recent deltas
   - Compute 5-bar rolling std of recent deltas
   - For buy: require current_delta > rolling_mean + 0.5 * rolling_std (extreme high)
   - For sell: require current_delta < rolling_mean - 0.5 * rolling_std (extreme low)
   - This enters when order flow reaches statistical extremes
   - More robust than momentum confirmation in choppy markets

3. Maintaining all proven parameters:
   - MA period 10, delta lookback 14, vol lookback 12
   - Confirmation bars 2 with majority rule
   - Base stop/target offset 0.75
   - Combined vol ratio [0.6, 1.8]
   - 70th percentile delta threshold

Key hypothesis:
- #36 is strong at 5.8422
- Mean reversion confirmation captures turning points
- Double confirmation (percentile + mean reversion) filters noise
- Should improve on #36 by entering at extremes rather than continuations
- More intuitive than momentum for order flow: extreme deltas revert
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
        reversion_std_mult: float = 0.5,
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
            reversion_std_mult: std multiplier for mean reversion (0.5 = 0.5 std from mean)
            size: contract size
            base_stop_offset: base protective stop distance
            base_target_offset: base profit target distance
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.confirmation_bars = confirmation_bars
        self.reversion_std_mult = reversion_std_mult
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
        # Track closes for moving average
        self.closes: deque[float] = deque(maxlen=ma_period)
        
        # Track deltas for distribution analysis
        self.deltas: deque[float] = deque(maxlen=delta_lookback)
        
        # Track bar ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track recent deltas for mean reversion (5-bar window)
        self.recent_deltas: deque[float] = deque(maxlen=5)
        
        # Track trend signals for multi-bar confirmation
        self.trend_signals: deque[int] = deque(maxlen=confirmation_bars)
        
        # Track recent price direction for secondary trend confirmation
        self.price_directions: deque[int] = deque(maxlen=3)
        
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

    def _check_mean_reversion(self, current_delta: float, signal: int) -> bool:
        """
        Check if delta is at an extreme relative to recent mean.
        Buy: require current_delta > mean + std_mult * std
        Sell: require current_delta < mean - std_mult * std
        """
        if signal == 0 or len(self.recent_deltas) < 3:
            return False
        
        deltas_list = list(self.recent_deltas)
        mean_delta = sum(deltas_list) / len(deltas_list)
        
        # Compute std
        variance = sum((d - mean_delta) ** 2 for d in deltas_list) / len(deltas_list)
        if variance <= 0:
            std_delta = 1.0
        else:
            std_delta = math.sqrt(variance)
        
        threshold = self.reversion_std_mult * std_delta
        
        if signal > 0:
            # Buy signal: require current_delta > mean + threshold (extreme high)
            return current_delta > mean_delta + threshold
        else:
            # Sell signal: require current_delta < mean - threshold (extreme low)
            return current_delta < mean_delta - threshold

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.recent_deltas.clear()
            self.trend_signals.clear()
            self.price_directions.clear()
        
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
            
            # Check mean reversion confirmation
            reversion_ok = self._check_mean_reversion(current_delta, signal)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # recent price action shows upward bias, mean reversion confirms
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                positive_price_dirs >= len(price_dirs_list) - 1 and
                reversion_ok):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # recent price action shows downward bias, mean reversion confirms
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  negative_price_dirs >= len(price_dirs_list) - 1 and
                  reversion_ok):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
