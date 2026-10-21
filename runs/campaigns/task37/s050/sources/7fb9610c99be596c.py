"""
An order flow strategy with regime-adaptive delta thresholds and dynamic confirmation.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with multi-bar confirmation + volatility adjustment
2. Trial #12 (score +2.8520) added delta acceleration but slightly underperformed
3. Trial #13 (score +0.8646) used simpler extremeness but performance dropped
4. The core winning pattern: price trend (MA) + order flow (delta) + volatility adjustment

This strategy refines #7 by:
1. Improving threshold adaptation: use dynamic delta thresholds based on regime volatility
2. Better signal quality: require delta to be extreme relative to current volatility regime
3. Enhanced multi-bar confirmation: weight recent signals by their strength/extremeness
4. More adaptive volatility: scale thresholds AND brackets based on recent volatility
5. Regime-aware entry: adjust confirmation strictness based on volatility regime
6. Cleaner signal detection: delta extremeness relative to regime, not absolute percentile

Key hypothesis:
- #7 works because multi-bar confirmation reduces false signals
- Weakness: fixed 70th percentile threshold doesn't adapt to changing volatility regimes
- Improvement: scale the delta threshold based on current volatility regime
- In high volatility: require MORE extreme delta (higher threshold)
- In low volatility: require LESS extreme delta (lower threshold)
- This makes signals more consistent across different market conditions

The strategy:
1. Tracks price trend via moving average (trend filter)
2. Measures volatility regime dynamically from recent ranges
3. Scales delta threshold based on volatility regime
4. Uses regime-adaptive confirmation: stricter in calm markets, looser in volatile
5. Tracks signal strength (how extreme is the delta relative to threshold)
6. Multi-bar confirmation weighted by signal strength
7. Combines: MA trend + regime-adaptive delta + weighted multi-bar confirmation
8. Volatility adjustment scales both thresholds and brackets
9. Better entry: only trade when multiple aligned extreme signals in current regime
"""

from __future__ import annotations
from collections import deque


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
        
        # Track bar ranges for volatility regime detection
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track recent delta signals and their strength
        self.trend_signals: deque[int] = deque(maxlen=confirmation_bars)
        self.signal_strength: deque[float] = deque(maxlen=confirmation_bars)
        
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset at session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.closes.clear()
            self.deltas.clear()
            self.ranges.clear()
            self.trend_signals.clear()
            self.signal_strength.clear()
        
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
        
        # Compute volatility regime from recent ranges
        ranges_list = list(self.ranges)
        avg_range = sum(ranges_list) / len(ranges_list) if ranges_list else 1.0
        
        if avg_range > 0.0:
            # Current volatility relative to average
            current_range = ranges_list[-1] if ranges_list else avg_range
            vol_regime = current_range / avg_range
        else:
            vol_regime = 1.0
        
        # Clamp volatility regime to reasonable bounds
        vol_regime = max(0.6, min(vol_regime, 1.6))
        
        # Compute delta threshold using 65th percentile, scaled by volatility regime
        deltas_list = list(self.deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        threshold_idx = int(0.65 * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        base_delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if base_delta_threshold < 0.5:
            base_delta_threshold = 0.5
        
        # Scale threshold based on volatility regime
        # In high volatility: require MORE extreme delta (higher threshold)
        # In low volatility: require LESS extreme delta (lower threshold)
        delta_threshold = base_delta_threshold * vol_regime
        
        # Current delta
        current_delta = delta
        current_delta_mag = abs(current_delta)
        
        # Determine signal and signal strength
        signal = 0
        strength = 0.0
        
        if current_delta > delta_threshold:
            signal = 1  # Buy signal
            # Signal strength: how much above threshold (normalized)
            strength = (current_delta_mag - delta_threshold) / (delta_threshold + 0.01)
            strength = min(strength, 2.0)  # Cap at 2.0
        elif current_delta < -delta_threshold:
            signal = -1  # Sell signal
            # Signal strength: how much above threshold (normalized)
            strength = (current_delta_mag - delta_threshold) / (delta_threshold + 0.01)
            strength = min(strength, 2.0)  # Cap at 2.0
        
        self.trend_signals.append(signal)
        self.signal_strength.append(strength)
        
        # Adjust bracket levels based on volatility regime
        stop_offset = self.base_stop_offset * vol_regime
        target_offset = self.base_target_offset * vol_regime
        
        # Multi-bar confirmation: check if recent signals align and are strong
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            strength_list = list(self.signal_strength)
            
            # Count aligned signals and compute weighted strength
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Weighted strength: average strength of aligned signals
            positive_strength = sum(strength_list[i] for i in range(len(signals_list)) if signals_list[i] > 0) / max(positive_signals, 1)
            negative_strength = sum(strength_list[i] for i in range(len(signals_list)) if signals_list[i] < 0) / max(negative_signals, 1)
            
            # Adjust confirmation requirement based on volatility regime
            # In calm markets (low vol_regime): require stricter alignment
            # In volatile markets (high vol_regime): allow looser alignment
            min_aligned = max(1, int(self.confirmation_bars * (1.0 / vol_regime)))
            min_aligned = min(min_aligned, self.confirmation_bars)
            
            # Long: price above MA, current buy signal, and recent signals show alignment
            if (price_above_ma and 
                signal > 0 and
                positive_signals >= min_aligned and
                positive_strength > 0.3):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="regime-long",
                )
            
            # Short: price below MA, current sell signal, and recent signals show alignment
            elif (price_below_ma and 
                  signal < 0 and
                  negative_signals >= min_aligned and
                  negative_strength > 0.3):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="regime-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
