"""
An order flow strategy with refined delta regime detection and adaptive confirmation.

Key insights from prior trials:
1. Trial #7, #19, #23, #25 (score +3.0520) - the baseline best:
   - 70th percentile delta threshold
   - Multi-bar confirmation (confirmation_bars=2)
   - Secondary price direction confirmation
   - Volatility scaling using 65th percentile of ranges
   - MA period of 10

2. Trial #26 (score +5.7681) - CURRENT BEST:
   - Adds delta volatility scaling to adapt threshold
   - Combines price volatility AND delta volatility ratios
   - Scales both threshold and bracket levels adaptively
   - Uses delta vol ratio in range [0.7, 1.3]
   - Combines vol_ratio * delta_vol_ratio for bracket scaling

3. Analysis of what makes #26 work better:
   - Delta volatility is a market regime indicator
   - Adapting the threshold based on delta vol improves signal quality
   - Scaling brackets by combined volatility ratio improves risk/reward
   - The delta_vol_ratio range [0.7, 1.3] is well-calibrated
   - Combined ratio clamped to [0.6, 1.8] provides good scaling bounds

This strategy refines #26 by:
1. Keeping the core adaptive threshold logic from #26 exactly
2. Improving regime detection:
   - Add a regime strength indicator based on delta distribution shape
   - Measure the concentration of deltas (how peaked vs flat the distribution is)
   - Use kurtosis-like metric to detect if we're in a strong regime
3. Refining confirmation requirements based on regime strength:
   - In strong regimes (concentrated deltas), require less confirmation
   - In weak regimes (dispersed deltas), require more confirmation
   - This adapts the strategy to market conditions dynamically
4. Adding a secondary signal filter:
   - Check if the current delta is in the direction we're trading
   - Ensure delta extremeness aligns with delta direction
   - Filter out whipsaws where delta is extreme but wrong-direction
5. Keeping all proven parameters:
   - MA period 10 (trend filter)
   - Delta lookback 14 (distribution measurement)
   - Vol lookback 12 (price volatility)
   - Base confirmation 2 (multi-bar alignment)
   - Adaptive threshold scaling from #26
   - Combined volatility ratio scaling from #26

Key hypothesis:
- #26 works because it adapts threshold and brackets to delta volatility
- We can improve by: also adapting confirmation requirements to regime strength
- Strong delta regimes (concentrated distribution) = fewer confirmations needed
- Weak delta regimes (dispersed distribution) = more confirmations needed
- Adding regime-aware confirmation maintains quality while capturing more trades
- The combination of: adaptive threshold + regime-aware confirmation + volatility scaling
  should improve on #26's score

The strategy:
1. Tracks price trend via moving average (MA period 10)
2. Measures delta volatility and regime strength (distribution concentration)
3. Scales delta threshold adaptively based on delta volatility
4. Adapts confirmation requirements based on regime strength
5. Ensures delta direction aligns with trade direction (secondary filter)
6. Scales both stop and target based on combined volatility ratio
7. Combines: MA trend + adaptive threshold + regime-aware confirmation + volatility scaling
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
        """
        self.interval = interval
        self.ma_period = ma_period
        self.delta_lookback = delta_lookback
        self.vol_lookback = vol_lookback
        self.confirmation_bars = confirmation_bars
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

    def _compute_regime_strength(self, deltas_list: list[float]) -> float:
        """
        Compute regime strength based on distribution concentration.
        Returns value in [0, 1] where 1 = strong concentrated regime, 0 = weak dispersed regime.
        """
        if len(deltas_list) < 2:
            return 0.5
        
        # Measure concentration using ratio of std dev to mean absolute value
        mean_abs_delta = sum(abs(d) for d in deltas_list) / len(deltas_list)
        std_dev = self._compute_delta_volatility(deltas_list)
        
        if mean_abs_delta <= 0:
            return 0.5
        
        # Lower ratio = more concentrated = stronger regime
        # Higher ratio = more dispersed = weaker regime
        concentration_ratio = std_dev / mean_abs_delta
        
        # Map to [0, 1] where 1 = strong regime (low ratio), 0 = weak regime (high ratio)
        # Assume typical range is 0.5 to 3.0
        regime_strength = max(0.0, min(1.0, 1.0 - (concentration_ratio - 0.5) / 2.5))
        
        return regime_strength

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
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
        
        # Compute regime strength for adaptive confirmation
        regime_strength = self._compute_regime_strength(deltas_list)
        
        # Compute delta threshold from percentile of absolute deltas
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # 70th percentile for threshold (proven in #7 and #26)
        threshold_idx = int(0.70 * (len(abs_deltas_sorted) - 1))
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
        
        # Adapt confirmation requirements based on regime strength
        # Strong regime (concentrated deltas) = less confirmation needed
        # Weak regime (dispersed deltas) = more confirmation needed
        required_confirmations = max(1, int(self.confirmation_bars * (1.5 - regime_strength)))
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= required_confirmations:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # delta direction aligns (positive), and recent price action shows upward bias
            if (price_above_ma and 
                positive_signals >= max(1, required_confirmations - 1) and 
                signal > 0 and
                current_delta > 0 and
                positive_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="confirmed-long",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # delta direction aligns (negative), and recent price action shows downward bias
            elif (price_below_ma and 
                  negative_signals >= max(1, required_confirmations - 1) and 
                  signal < 0 and
                  current_delta < 0 and
                  negative_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="confirmed-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
