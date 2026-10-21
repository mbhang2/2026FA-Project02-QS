"""
An order flow strategy with refined signal quality through adaptive entry timing.

Key insights from prior trials:

1. Trial #62 (score +5.3606) - RECENT:
   - Weighted scoring for entry confirmation (score >= 4 of 5)
   - Improved capture rate vs strict AND logic
   - BUT: score decreased from #60's 5.8968
   - Hypothesis: weighted scoring was TOO permissive

2. Trial #60 (score +5.8968) - BEST:
   - Smooth volatility-adaptive momentum threshold
   - Strict AND logic (all 5 conditions required)
   - 70th percentile delta threshold
   - Multi-bar confirmation with strict price direction
   - MA period 10, delta_lookback 14, vol_lookback 12

3. Core pattern analysis:
   - #60 at 5.8968 is the peak with strict AND logic
   - #62 at 5.3606 shows weighted scoring (>= 4) was too loose
   - The issue: relaxing confirmation reduced signal quality
   - Real insight: the STRICTNESS of #60 is what works
   - All 5 conditions together create a HIGH-QUALITY filter

4. New hypothesis for improvement over #60:
   - #60 is strong at 5.8968 - strict confirmation works
   - Opportunity: ADAPTIVE ENTRY THRESHOLDS based on market regime
   - Current: fixed 70th percentile delta threshold
   - Issue: same threshold applies to all market conditions
   - New approach: MARKET REGIME DETECTION
   - Detect regime via recent volatility and delta volatility
   - In QUIET regime (low vol, low delta_vol):
     - Use HIGHER delta threshold (75th percentile) - stricter entries
     - Require HIGHER momentum threshold (0.6x) - strict confirmation
     - These quiet markets need stricter confirmation
   - In NORMAL regime (medium vol/delta_vol):
     - Use 70th percentile - baseline from #60
     - Use standard momentum thresholds
   - In VOLATILE regime (high vol, high delta_vol):
     - Use LOWER delta threshold (65th percentile) - looser entries
     - Use LOWER momentum threshold (0.3x) - easier confirmation
     - Volatile markets move faster, need looser confirmation
   - This adapts signal sensitivity to market regime
   - Should improve consistency across different conditions

5. Market regime adaptation approach:
   - Compute combined volatility score from price_vol and delta_vol
   - If combined_vol < 0.8: QUIET regime
     - delta_threshold: 75th percentile (stricter)
     - momentum_threshold: 0.6 (stricter)
   - If combined_vol 0.8-1.2: NORMAL regime
     - delta_threshold: 70th percentile (baseline)
     - momentum_threshold: 0.5 (baseline)
   - If combined_vol > 1.2: VOLATILE regime
     - delta_threshold: 65th percentile (looser)
     - momentum_threshold: 0.4 (looser)
   - Use smooth interpolation at boundaries to avoid regime flipping
   - Hypothesis: regime-adaptive thresholds improve consistency

This strategy refines #60 by:
1. Keeping all core logic from #60 exactly:
   - Smooth volatility-adaptive momentum threshold
   - Combined volatility ratio scaling [0.6, 1.8]
   - MA period 10, delta_lookback 14, vol_lookback 12
   - Strict AND logic for all 5 confirmation conditions
   - Multi-bar confirmation with majority rule

2. Adding market regime detection:
   - Compute combined volatility regime score
   - Adapt delta percentile threshold to regime
   - Adapt momentum threshold multiplier to regime
   - Use smooth interpolation at regime boundaries
   - This makes signal sensitivity adaptive to market conditions

3. Maintaining all proven parameters:
   - MA period 10, delta lookback 14, vol lookback 12
   - Confirmation bars 2 with majority rule
   - Base stop/target offset 0.75
   - Combined vol ratio [0.6, 1.8]
   - Strict AND logic for all 5 conditions

Key hypothesis:
- #60 is strong at 5.8968 with strict AND logic
- Market regime detection improves consistency
- Quiet markets need stricter confirmation (75th, 0.6x)
- Normal markets use baseline (70th, 0.5x)
- Volatile markets need looser confirmation (65th, 0.4x)
- Smooth transitions prevent regime-boundary artifacts
- Should improve robustness across different market conditions
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

    def _get_regime_adaptive_percentile(self, combined_vol: float) -> float:
        """
        Get percentile for delta threshold adapted to market regime.
        
        combined_vol < 0.8: 0.75 (quiet regime, stricter)
        combined_vol 0.8-1.2: interpolate 0.75 → 0.70 → 0.65
        combined_vol > 1.2: 0.65 (volatile regime, looser)
        """
        if combined_vol < 0.8:
            return 0.75
        elif combined_vol < 1.2:
            # Linear interpolation from 0.75 to 0.65 over [0.8, 1.2]
            t = (combined_vol - 0.8) / (1.2 - 0.8)
            return 0.75 - t * 0.10
        else:
            return 0.65

    def _get_regime_adaptive_momentum(self, combined_vol: float) -> float:
        """
        Get momentum threshold adapted to market regime.
        
        combined_vol < 0.8: 0.6 (quiet regime, stricter)
        combined_vol 0.8-1.2: interpolate 0.6 → 0.5 → 0.4
        combined_vol > 1.2: 0.4 (volatile regime, looser)
        """
        if combined_vol < 0.8:
            return 0.6
        elif combined_vol < 1.2:
            # Linear interpolation from 0.6 to 0.4 over [0.8, 1.2]
            t = (combined_vol - 0.8) / (1.2 - 0.8)
            return 0.6 - t * 0.2
        else:
            return 0.4

    def _get_smooth_adaptive_momentum(self, vol_ratio: float) -> float:
        """
        Get momentum threshold adapted to volatility regime with smooth transitions.
        
        vol_ratio < 0.7: 0.6 (strict, low volatility)
        vol_ratio 0.7-0.9: linear interpolate 0.6 → 0.5
        vol_ratio 0.9-1.1: 0.5 (baseline, normal volatility)
        vol_ratio 1.1-1.3: linear interpolate 0.5 → 0.4
        vol_ratio > 1.3: 0.4 (loose, high volatility)
        """
        if vol_ratio < 0.7:
            return 0.6
        elif vol_ratio < 0.9:
            # Linear interpolation from 0.6 to 0.5 over [0.7, 0.9]
            t = (vol_ratio - 0.7) / (0.9 - 0.7)
            return 0.6 - t * 0.1
        elif vol_ratio < 1.1:
            return 0.5
        elif vol_ratio < 1.3:
            # Linear interpolation from 0.5 to 0.4 over [1.1, 1.3]
            t = (vol_ratio - 1.1) / (1.3 - 1.1)
            return 0.5 - t * 0.1
        else:
            return 0.4

    def _check_momentum(self, current_delta: float, signal: int, adaptive_threshold: float) -> bool:
        """
        Check if delta momentum aligns with signal direction.
        Uses adaptive threshold based on volatility regime.
        """
        if signal == 0:
            return False
        
        if signal > 0:
            # Buy signal: require current_delta > threshold * previous_delta
            return current_delta > adaptive_threshold * self.prev_delta
        else:
            # Sell signal: require current_delta < threshold * previous_delta
            return current_delta < adaptive_threshold * self.prev_delta

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
        
        # Compute combined volatility for market regime detection
        delta_vol_ratio = max(0.7, min(delta_vol / 50.0, 1.3))
        combined_vol = vol_ratio * delta_vol_ratio
        combined_vol = max(0.6, min(combined_vol, 1.8))
        
        # Get regime-adaptive delta percentile
        regime_percentile = self._get_regime_adaptive_percentile(combined_vol)
        
        # Compute delta threshold from regime-adaptive percentile
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        threshold_idx = int(regime_percentile * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Adapt threshold based on delta volatility
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
        
        # Get regime-adaptive momentum threshold
        regime_momentum = self._get_regime_adaptive_momentum(combined_vol)
        
        # Also apply price volatility-based smooth adaptation
        smooth_momentum = self._get_smooth_adaptive_momentum(vol_ratio)
        
        # Use the more conservative (higher) of the two thresholds
        adaptive_momentum = max(regime_momentum, smooth_momentum * 0.8)
        
        # Scale bracket levels based on combined volatility
        stop_offset = self.base_stop_offset * combined_vol
        target_offset = self.base_target_offset * combined_vol
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Check adaptive momentum confirmation
            momentum_ok = self._check_momentum(current_delta, signal, adaptive_momentum)
            
            # Long: ALL 5 conditions required (strict AND logic from #60)
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
            
            # Short: ALL 5 conditions required (strict AND logic from #60)
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
