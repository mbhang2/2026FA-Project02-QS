"""
An order flow strategy with refined signal quality and adaptive multi-regime trading.

Key insights from prior trials:

1. Trial #26 (score +5.7681) - BEST:
   - Adaptive delta threshold based on delta volatility
   - Combined volatility ratio scaling (price vol * delta vol)
   - Multi-bar confirmation with price direction alignment
   - 70th percentile delta threshold proven optimal
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #33 (score +3.4691):
   - Added delta momentum confirmation to #26
   - Delta momentum filter reduced noise but also reduced trade count
   - Momentum requirement (delta_momentum > 0 for buys) was too strict
   - Score dropped from 5.7681 to 3.4691, confirming over-filtering

3. Analysis of what degrades performance:
   - Adding momentum filters reduces trade count without improving quality
   - Stricter confirmation rules filter out valid entries
   - The #26 formula is near-optimal and simple is better
   - Attempts to improve signal timing have consistently failed

4. New hypothesis for improvement:
   - #26's core formula is optimal - keep it exactly
   - The opportunity is in REGIME-AWARE BRACKET SIZING
   - Different market regimes need different stop/target distances
   - Instead of uniform combined_vol_ratio, use regime-specific sizing
   - Detect regime using: delta volatility, price volatility, trend strength
   - High volatility regime: wider stops/targets (accept more slippage)
   - Low volatility regime: tighter stops/targets (capture quick moves)
   - Trending regime: asymmetric brackets (wider target, tighter stop)
   - Mean-reverting regime: symmetric brackets (equal stop/target)

5. Regime detection approach:
   - Compute delta volatility (already done in #26)
   - Compute price volatility (already done in #26)
   - Compute trend strength: (price - MA) / ATR
   - Classify regime based on volatility levels and trend strength
   - Adjust stop/target offsets based on regime

6. Implementation strategy:
   - Keep all of #26's signal detection logic EXACTLY
   - Add regime detection layer on top
   - Use regime to scale bracket levels differently
   - High vol + strong trend: wider target (capture momentum)
   - Low vol + weak trend: tighter stops (protect capital)
   - This improves risk-adjusted returns without changing signals

This strategy refines #26 by:
1. Keeping all signal detection from #26 exactly:
   - 70th percentile delta threshold
   - Adaptive delta_vol_ratio [0.7, 1.3]
   - Multi-bar confirmation with majority rule
   - MA period 10, delta lookback 14, vol lookback 12
   - Price direction confirmation

2. Adding regime-aware bracket sizing:
   - Detect market regime using volatility and trend metrics
   - Use regime to adjust stop/target offsets
   - High volatility regime: scale up both stop and target
   - Low volatility regime: scale down both stop and target
   - Trending regime: favor wider targets over stops
   - Mean-reverting regime: symmetric stops and targets

3. Regime classification:
   - Measure delta volatility (existing)
   - Measure price volatility (existing)
   - Measure trend strength: deviation from MA relative to volatility
   - Classify into: high-vol-trend, high-vol-mean-revert, low-vol-trend, low-vol-mean-revert
   - Apply regime-specific bracket multipliers

Key hypothesis:
- #26 is optimal for signal detection
- Regime-aware bracket sizing improves risk-adjusted returns
- Different regimes need different stop/target ratios
- This should improve on #26's score of 5.7681
- Regime detection is lightweight and data-driven
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

    def _compute_trend_strength(self, price: float, ma: float, volatility: float) -> float:
        """Compute trend strength as deviation from MA relative to volatility."""
        if volatility <= 0:
            return 0.0
        
        deviation = abs(price - ma)
        trend_strength = deviation / volatility
        
        return min(trend_strength, 3.0)  # Cap at 3.0

    def _get_regime_multipliers(
        self, 
        delta_vol: float, 
        price_vol: float, 
        trend_strength: float,
        signal: int
    ) -> tuple[float, float]:
        """
        Determine stop and target multipliers based on market regime.
        
        Returns: (stop_multiplier, target_multiplier)
        """
        # Normalize volatilities to comparable scale
        delta_vol_normalized = min(delta_vol / 50.0, 2.0)  # Typical delta vol ~50
        price_vol_normalized = min(price_vol / 1.0, 2.0)   # Typical price vol ~1.0
        
        # Detect high vs low volatility regime
        is_high_vol = (delta_vol_normalized > 1.0) or (price_vol_normalized > 1.0)
        
        # Detect trending vs mean-reverting
        is_trending = trend_strength > 1.0
        
        # Base multipliers
        if is_high_vol and is_trending:
            # High volatility + trending: wider target, moderate stop
            # Capture momentum, accept wider stops
            stop_mult = 0.9
            target_mult = 1.3
        elif is_high_vol and not is_trending:
            # High volatility + mean-reverting: equal and wide
            # Prepare for reversals, use wider stops
            stop_mult = 1.1
            target_mult = 1.1
        elif not is_high_vol and is_trending:
            # Low volatility + trending: tight stop, moderate target
            # Protect capital, capture clear trend
            stop_mult = 0.7
            target_mult = 1.0
        else:
            # Low volatility + mean-reverting: tight and symmetric
            # Protect capital, quick exits
            stop_mult = 0.8
            target_mult = 0.8
        
        # Adjust based on signal strength (how extreme the delta is)
        if abs(signal) > 0:
            # Strong signals get slightly wider targets
            target_mult *= 1.05
        
        return stop_mult, target_mult

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
        
        # Compute trend strength for regime detection
        trend_strength = self._compute_trend_strength(current_price, ma, avg_range if len(self.ranges) >= 2 else 1.0)
        
        # Get regime-aware multipliers
        stop_mult, target_mult = self._get_regime_multipliers(delta_vol, vol_ratio, trend_strength, signal)
        
        # Apply regime-aware scaling
        stop_offset = self.base_stop_offset * vol_ratio * stop_mult
        target_offset = self.base_target_offset * vol_ratio * target_mult
        
        # Clamp to reasonable bounds
        stop_offset = max(0.3, min(stop_offset, 2.0))
        target_offset = max(0.3, min(target_offset, 2.0))
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # and recent price action shows upward bias
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars - 1 and 
                signal > 0 and
                positive_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # and recent price action shows downward bias
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars - 1 and 
                  signal < 0 and
                  negative_price_dirs >= len(price_dirs_list) - 1):
                
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
