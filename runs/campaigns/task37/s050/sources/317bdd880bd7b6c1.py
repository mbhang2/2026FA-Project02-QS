"""
An order flow strategy with refined delta signal detection and improved confirmation.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with:
   - 70th percentile delta threshold
   - Multi-bar confirmation (confirmation_bars=2)
   - Secondary price direction confirmation
   - Volatility scaling using 65th percentile of ranges

2. Trial #15 (recent) attempted to add delta momentum but underperformed
   - The momentum calculation may have been too strict
   - Requiring momentum >= confirmation_bars - 1 with strict price alignment was too restrictive

3. Trial #14 (score +0.8646) failed with regime-adaptive thresholds
   - Scaling threshold by vol_regime made signals worse
   - Fixed percentile approach is better

Analysis of what works:
- The 70th percentile delta threshold is the right selectivity level
- Multi-bar signal alignment is crucial (reduces false signals)
- Price direction confirmation helps filter choppy markets
- Volatility scaling should use percentile-based approach (not regime-adaptive)
- Secondary confirmation should be present but not overly strict

This strategy refines #7 by:
1. Keeping the core winning formula: 70th percentile delta threshold
2. Slightly loosening the secondary price direction requirement
3. Using a simpler multi-bar confirmation (don't require ALL bars to align)
4. Better handling of transition periods: allow entry when trend aligns + current signal strong
5. Refining the confirmation logic: require 1+ aligned signals instead of all
6. Keeping volatility scaling simple: 65th percentile of ranges (proven in #7)
7. Adding robustness: require current bar to have strong signal, but recent bars don't all need to

Key hypothesis:
- #7 works because the balance of selectivity is right
- The secondary price direction confirmation in #7 is good but maybe too strict
- We can improve by: slightly loosening confirmation while keeping core logic
- Allow entry when: MA aligned + current signal extreme + at least 1 recent signal aligned
- This keeps the selectivity but allows more opportunities in trending markets

The strategy:
1. Tracks price trend via moving average (like #7)
2. Uses 70th percentile delta threshold (proven in #7)
3. Requires strong multi-bar confirmation with relaxed requirements
4. Adds price direction confirmation: at least 1 recent bar shows alignment
5. Scales volatility using 65th percentile (like #7)
6. Combines: MA trend + delta percentile + multi-bar alignment (relaxed) + price direction
7. Entry when: MA aligned + current signal extreme + recent signals show some alignment
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
            confirmation_bars: number of bars to check for confirmation
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
        
        # Compute delta threshold from 70th percentile of absolute deltas
        deltas_list = list(self.deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        abs_deltas_sorted = sorted(abs_deltas)
        
        # 70th percentile for threshold (proven in #7)
        threshold_idx = int(0.70 * (len(abs_deltas_sorted) - 1))
        threshold_idx = max(0, min(threshold_idx, len(abs_deltas_sorted) - 1))
        delta_threshold = abs_deltas_sorted[threshold_idx]
        
        # Ensure minimum threshold
        if delta_threshold < 0.5:
            delta_threshold = 0.5
        
        # Current delta
        current_delta = delta
        
        # Determine signal based on delta extremeness
        signal = 0
        if current_delta > delta_threshold:
            signal = 1  # Strong buy signal
        elif current_delta < -delta_threshold:
            signal = -1  # Strong sell signal
        
        self.trend_signals.append(signal)
        
        # Compute volatility adjustment using 65th percentile (like #7)
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            # Use 65th percentile of ranges (like #7)
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
        
        # Adjust bracket levels based on volatility
        stop_offset = self.base_stop_offset * vol_ratio
        target_offset = self.base_target_offset * vol_ratio
        
        # Multi-bar confirmation: check if recent signals show alignment
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Long: price above MA, current buy signal, and at least 1 recent signal aligned
            # Also require at least 1 recent bar showing upward price movement
            if (price_above_ma and 
                signal > 0 and
                positive_signals >= 1 and
                positive_price_dirs >= 1):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry-long",
                )
            
            # Short: price below MA, current sell signal, and at least 1 recent signal aligned
            # Also require at least 1 recent bar showing downward price movement
            elif (price_below_ma and 
                  signal < 0 and
                  negative_signals >= 1 and
                  negative_price_dirs >= 1):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
