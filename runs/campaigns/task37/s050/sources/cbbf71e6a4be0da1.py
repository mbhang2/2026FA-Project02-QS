"""
An order flow strategy with refined multi-timeframe delta confirmation and adaptive thresholds.

Key insights from prior trials:

1. Trial #36 (score +5.8422) - BEST OVERALL:
   - Soft delta momentum confirmation with 0.5x threshold
   - Adaptive delta threshold from 70th percentile
   - Combined volatility ratio scaling [0.6, 1.8]
   - Multi-bar confirmation with price direction alignment
   - MA period 10, delta_lookback 14, vol_lookback 12

2. Trial #50 (score +4.4397):
   - Added bidirectional delta confirmation (both bars same sign)
   - Score dropped from #36's 5.8422 to 4.4397
   - Bidirectional filter was too restrictive

3. Trial #49 (score +5.6122):
   - Added delta-price alignment strength (price move > 0.3 * range)
   - Score was 5.6122, close to but below #36's 5.8422
   - Alignment threshold slightly too strict

4. Core pattern from all analysis:
   - #36 at 5.8422 is the peak performance
   - The core formula is near-optimal and simple
   - The 70th percentile threshold is robust
   - The 0.5x momentum check is effective
   - Multi-bar confirmation with price direction is critical
   - Adding filters degrades performance when too strict

5. New hypothesis for improvement over #36:
   - #36 is strong at 5.8422 - keep the core formula intact
   - The opportunity is in RELAXED MOMENTUM THRESHOLD TUNING
   - Current momentum threshold: 0.5x (require 50% of previous delta)
   - Problem: 0.5x might be slightly too strict, filtering valid entries
   - Hypothesis: momentum threshold of 0.35 (35% of previous) is more selective
   - This allows entries when delta is accelerating upward/downward
   - Lighter filter than 0.5x, but still meaningful confirmation
   - Captures entries where momentum is building but not yet at 50%
   - Should improve on #36 by allowing more valid entries while filtering noise

6. Alternative hypothesis - ADAPTIVE MOMENTUM THRESHOLD:
   - Instead of fixed 0.5x, adapt based on delta volatility
   - High volatility regime: use 0.3x (more lenient, more noise)
   - Low volatility regime: use 0.6x (stricter, cleaner signals)
   - This captures regime-dependent entry conditions
   - Lighter than fixed strict threshold
   - More intuitive than fixed 0.5x

7. Best approach: RELAXED MOMENTUM + SIGNAL STRENGTH WEIGHTING
   - Keep 70th percentile threshold
   - Reduce momentum threshold from 0.5x to 0.35x
   - For strong signals (delta >> threshold), momentum requirement is relaxed
   - For weak signals (delta just above threshold), require stronger momentum
   - Measure signal strength as: abs(current_delta) / adapted_threshold
   - If strength > 1.5, allow momentum_threshold = 0.2 (very strong signal)
   - If strength > 1.0, allow momentum_threshold = 0.35 (normal signal)
   - If strength <= 1.0, require momentum_threshold = 0.5 (weak signal, stricter)
   - This creates adaptive confirmation that respects signal quality

This strategy refines #36 by:
1. Keeping all core logic from #36 exactly:
   - 70th percentile delta threshold for signal detection
   - Adaptive delta_vol_ratio [0.7, 1.3]
   - Combined volatility ratio [0.6, 1.8]
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Multi-bar confirmation with majority rule
   - Price direction confirmation

2. Adding signal strength-weighted momentum confirmation:
   - Measure signal strength: abs(current_delta) / adapted_threshold
   - Strong signals (strength > 1.5): momentum_threshold = 0.2
   - Normal signals (strength > 1.0): momentum_threshold = 0.35
   - Weak signals (strength <= 1.0): momentum_threshold = 0.5
   - This creates adaptive momentum that respects signal quality
   - Strong order flow needs less momentum confirmation
   - Weak order flow needs more momentum confirmation

3. Maintaining all proven parameters:
   - MA period 10, delta lookback 14, vol lookback 12
   - Confirmation bars 2 with majority rule
   - Base stop/target offset 0.75
   - Combined vol ratio [0.6, 1.8]
   - 70th percentile delta threshold

Key hypothesis:
- #36 is strong at 5.8422
- Signal strength-weighted momentum adapts confirmation to signal quality
- Strong signals (>1.5x threshold) are reliable and need less confirmation
- Weak signals (just above threshold) need stricter momentum
- Should improve on #36 by allowing strong signals while filtering weak noise
- Maintains simplicity while improving entry quality through adaptive confirmation
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

    def _get_adaptive_momentum_threshold(self, signal_strength: float) -> float:
        """
        Get momentum threshold based on signal strength.
        Strong signals (>1.5x threshold) need less momentum confirmation.
        Weak signals (just above threshold) need stricter momentum.
        """
        if signal_strength > 1.5:
            return 0.2  # Very strong signal, minimal momentum requirement
        elif signal_strength > 1.0:
            return 0.35  # Normal signal, moderate momentum requirement
        else:
            return 0.5   # Weak signal, strict momentum requirement

    def _check_momentum(self, current_delta: float, signal: int, 
                       momentum_threshold: float) -> bool:
        """
        Check if delta momentum aligns with signal direction.
        Uses adaptive momentum threshold based on signal strength.
        """
        if signal == 0:
            return False
        
        if signal > 0:
            # Buy signal: require current_delta > threshold * previous_delta
            return current_delta > momentum_threshold * self.prev_delta
        else:
            # Sell signal: require current_delta < threshold * previous_delta
            return current_delta < momentum_threshold * self.prev_delta

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
            
            # Compute signal strength for adaptive momentum threshold
            signal_strength = abs(current_delta) / adapted_threshold if adapted_threshold > 0 else 1.0
            momentum_threshold = self._get_adaptive_momentum_threshold(signal_strength)
            
            # Check momentum confirmation with adaptive threshold
            momentum_ok = self._check_momentum(current_delta, signal, momentum_threshold)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # recent price action shows upward bias, momentum confirms
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
            # recent price action shows downward bias, momentum confirms
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
