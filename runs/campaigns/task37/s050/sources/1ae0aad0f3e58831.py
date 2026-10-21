"""
An order flow strategy with improved delta signal detection and adaptive confirmation.

Key insights from prior trials:
1. Trial #7 (score +3.0520, best) succeeded with multi-bar confirmation + volatility adjustment
2. Trial #14 (score +0.8646) added regime-adaptive thresholds but performance dropped significantly
3. Trial #13 (score +0.8646) simplified to extremeness but also underperformed
4. The core winning pattern: price trend (MA) + order flow (delta) + volatility adjustment

Analysis of #7's success:
- Uses 70th percentile delta threshold (more selective than #5's 65th)
- Requires multi-bar signal alignment (confirmation_bars=2)
- Adds secondary price direction confirmation (recent bars must show alignment)
- Scales volatility based on 65th percentile of ranges
- Combines: MA trend + delta percentile + multi-bar alignment + price direction

Why #14 failed:
- Regime-adaptive thresholds (scaling by vol_regime) made signal detection worse
- Complex weighted strength calculation didn't help
- The fixed percentile approach in #7 was actually better

Why #13 failed:
- Simpler extremeness detection reduced signal quality
- Removing the secondary price direction confirmation hurt performance
- The 65th percentile was too loose compared to #7's 70th

This strategy refines #7 by:
1. Keeping the core winning formula: 70th percentile delta threshold
2. Improving confirmation logic: require stronger alignment of signals
3. Better handling of low-signal periods: add a "signal strength" metric
4. More aggressive entry filtering: require both delta extremeness AND price alignment
5. Refining volatility scaling: use 70th percentile like #7 (not 65th like #13)
6. Adding signal momentum: track if delta is getting more extreme (not just if it's extreme)
7. Stricter multi-bar confirmation: require 2+ aligned signals with stronger price alignment
8. Better state management: cleaner tracking of trend signals and price directions

Key hypothesis:
- #7 works because the 70th percentile threshold is the right level of selectivity
- Secondary price direction confirmation is crucial for filtering false signals
- We can improve by: making confirmation stricter + adding signal momentum
- Signal momentum: track if delta is becoming more extreme (improving signal quality)
- This reduces whipsaw trades in choppy markets while catching strong moves

The strategy:
1. Tracks price trend via moving average (like #7)
2. Uses 70th percentile delta threshold (proven in #7)
3. Measures delta momentum: is current delta more extreme than recent bars?
4. Requires strong multi-bar confirmation with momentum alignment
5. Adds price direction confirmation: recent bars must show consistent direction
6. Scales volatility using 70th percentile (like #7, better than 65th)
7. Combines: MA trend + delta percentile + delta momentum + multi-bar alignment
8. Entry only when: MA aligned + current signal extreme + recent signals aligned + momentum positive
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
        
        # Track bar ranges for volatility
        self.ranges: deque[float] = deque(maxlen=vol_lookback)
        
        # Track trend signals for multi-bar confirmation
        self.trend_signals: deque[int] = deque(maxlen=confirmation_bars)
        
        # Track recent price direction for secondary trend confirmation
        self.price_directions: deque[int] = deque(maxlen=3)
        
        # Track delta momentum: is delta getting more extreme?
        self.delta_momentum: deque[float] = deque(maxlen=confirmation_bars)
        
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
            self.delta_momentum.clear()
        
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
        
        # Compute delta threshold from 70th percentile of absolute deltas (like #7)
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
        current_delta_mag = abs(current_delta)
        
        # Compute delta momentum: is current delta more extreme than recent average?
        if len(deltas_list) >= 2:
            recent_deltas = deltas_list[-3:]  # Last 3 deltas
            recent_avg_mag = sum(abs(d) for d in recent_deltas) / len(recent_deltas)
            momentum = (current_delta_mag - recent_avg_mag) / (recent_avg_mag + 0.01)
            momentum = max(-1.0, min(momentum, 2.0))  # Clamp momentum
        else:
            momentum = 0.0
        
        self.delta_momentum.append(momentum)
        
        # Determine signal based on delta extremeness
        signal = 0
        if current_delta > delta_threshold:
            signal = 1  # Strong buy signal
        elif current_delta < -delta_threshold:
            signal = -1  # Strong sell signal
        
        self.trend_signals.append(signal)
        
        # Compute volatility adjustment using 70th percentile (like #7)
        if len(self.ranges) >= 2:
            ranges_list = list(self.ranges)
            ranges_sorted = sorted(ranges_list)
            
            # Use 70th percentile of ranges (like #7)
            vol_idx = int(0.70 * (len(ranges_sorted) - 1))
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
        
        # Multi-bar confirmation: check if recent signals align with trend
        if len(self.trend_signals) >= self.confirmation_bars:
            signals_list = list(self.trend_signals)
            momentum_list = list(self.delta_momentum)
            
            positive_signals = sum(1 for s in signals_list if s > 0)
            negative_signals = sum(1 for s in signals_list if s < 0)
            
            # Check momentum alignment: recent momentum should be positive for long, negative for short
            positive_momentum = sum(1 for m in momentum_list if m > 0)
            negative_momentum = sum(1 for m in momentum_list if m < 0)
            
            # Secondary trend confirmation: check recent price direction
            price_dirs_list = list(self.price_directions)
            positive_price_dirs = sum(1 for d in price_dirs_list if d > 0)
            negative_price_dirs = sum(1 for d in price_dirs_list if d < 0)
            
            # Long: price above MA, strong positive signal alignment, current signal positive,
            # recent momentum shows buying pressure, and price action is upward
            if (price_above_ma and 
                positive_signals >= self.confirmation_bars and
                signal > 0 and
                positive_momentum >= self.confirmation_bars - 1 and
                positive_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="momentum-long",
                )
            
            # Short: price below MA, strong negative signal alignment, current signal negative,
            # recent momentum shows selling pressure, and price action is downward
            elif (price_below_ma and 
                  negative_signals >= self.confirmation_bars and
                  signal < 0 and
                  negative_momentum >= self.confirmation_bars - 1 and
                  negative_price_dirs >= len(price_dirs_list) - 1):
                
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="momentum-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
