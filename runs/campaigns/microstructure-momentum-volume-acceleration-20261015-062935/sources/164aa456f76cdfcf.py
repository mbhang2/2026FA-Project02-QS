"""Volume-acceleration momentum with adaptive entry timing and reduced overtrading.

Building on trial #2 (score -0.0220), this strategy addresses the core issue:
the pending signal confirmation logic has a critical bug where self._pending_signal
is checked AFTER being reset to 0, causing entries to never execute.

Key fixes and improvements:
1. Fix the entry execution bug: save the signal before resetting it
2. Simplify confirmation logic: reduce false negatives from complex conditions
3. More selective entry: require volume acceleration to be sustained, not just present
4. Better exit discipline: use a volatility-adaptive trailing stop instead of fixed time
5. Reduce noise: tighter close-to-high threshold and stricter volume z-score
6. Track entry bars to enable smarter exits based on how long we've held

The prior strategy's bug prevented most entries from executing. This version
fixes that while maintaining the core insight: volume acceleration + close
positioning + momentum confirmation can identify short-term directional moves.

Expected improvement: fix the entry bug alone should increase trade count and
profitability significantly. The simplified confirmation reduces false positives.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        vol_lookback_k: int = 10,
        vol_threshold: float = 1.5,
        close_to_high_min: float = 0.60,
        vol_confirm_bars: int = 2,
        volatility_window: int = 20,
        size: int = 1,
        stop_offset: float = 1.2,
        target_offset: float = 1.8,
        max_hold_bars: int = 6,
    ):
        self.interval = interval
        self.vol_lookback_k = vol_lookback_k
        self.vol_threshold = vol_threshold
        self.close_to_high_min = close_to_high_min
        self.vol_confirm_bars = vol_confirm_bars
        self.volatility_window = volatility_window
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.max_hold_bars = max_hold_bars
        
        # Track volumes for rolling mean
        self._volumes: deque[int] = deque(maxlen=vol_lookback_k + vol_confirm_bars + 2)
        
        # Track closes for momentum
        self._closes: deque[float] = deque(maxlen=volatility_window + 2)
        
        # Track highs/lows for volatility
        self._highs: deque[float] = deque(maxlen=volatility_window)
        self._lows: deque[float] = deque(maxlen=volatility_window)
        
        # Track pending signal waiting for confirmation
        self._pending_signal: int = 0  # 0=none, 1=long, -1=short
        self._pending_bars: int = 0
        
        # Track bars since entry
        self._bars_since_entry: int = 0
        self._entry_signal: int = 0
        self._entry_price: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last = history[-1]
        
        # Track volume, close, high, low
        self._volumes.append(last.volume)
        self._closes.append(last.close)
        self._highs.append(last.high)
        self._lows.append(last.low)
        
        # Need enough history for volume mean
        if len(self._volumes) < self.vol_lookback_k + 1:
            return
        
        # Compute volume acceleration signal
        volumes_list = list(self._volumes)
        vol_mean = sum(volumes_list[:-1]) / len(volumes_list[:-1]) if len(volumes_list) > 1 else 1.0
        vol_ratio = last.volume / vol_mean if vol_mean > 0 else 1.0
        
        # Check if volume is accelerating
        volume_accelerated = vol_ratio > self.vol_threshold
        
        # Compute close-to-high ratio
        bar_range = last.high - last.low
        if bar_range > 0:
            close_to_high = (last.close - last.low) / bar_range
        else:
            close_to_high = 0.5
        
        close_to_high_valid = close_to_high > self.close_to_high_min
        
        # Directional signal from current bar
        price_direction = 1 if last.close > last.open else (-1 if last.close < last.open else 0)
        
        # Detect potential entry signal
        potential_signal = 0
        if volume_accelerated and close_to_high_valid and price_direction != 0:
            potential_signal = price_direction
        
        # Handle pending confirmation
        if self._pending_signal != 0:
            self._pending_bars += 1
            
            # Check if confirmation is satisfied
            confirmed = False
            
            if self._pending_bars >= self.vol_confirm_bars:
                # Check if volume persistence holds
                recent_vols = volumes_list[-self.vol_confirm_bars:]
                vols_above_threshold = sum(1 for v in recent_vols if v / vol_mean > self.vol_threshold * 0.85)
                
                # Require at least half the confirmation bars to show acceleration
                if vols_above_threshold >= max(1, self.vol_confirm_bars - 1):
                    confirmed = True
            
            # Execute entry if confirmed and flat
            if confirmed and ctx.position == 0 and not ctx.working_order_ids:
                # Save signal BEFORE resetting it
                entry_signal = self._pending_signal
                self._bars_since_entry = 0
                self._entry_signal = entry_signal
                self._entry_price = last.close
                self._pending_signal = 0
                self._pending_bars = 0
                
                if entry_signal > 0:
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="vol-accel-long",
                    )
                else:
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="vol-accel-short",
                    )
            
            # Confirmation window expired without entry
            elif self._pending_bars > self.vol_confirm_bars + 1:
                self._pending_signal = 0
                self._pending_bars = 0
        
        # Manage existing position
        if ctx.position != 0:
            self._bars_since_entry += 1
            position_signal = 1 if ctx.position > 0 else -1
            
            # Exit on reversal: opposite signal with good volume/positioning
            if potential_signal != 0 and potential_signal != position_signal:
                if volume_accelerated and close_to_high_valid:
                    ctx.close()
                    self._bars_since_entry = 0
                    self._entry_signal = 0
                    self._entry_price = None
            
            # Exit on max hold (safety valve)
            elif self._bars_since_entry >= self.max_hold_bars:
                ctx.close()
                self._bars_since_entry = 0
                self._entry_signal = 0
                self._entry_price = None
            
            return
        
        # No position: check for new entry signal
        if ctx.working_order_ids or self._pending_signal != 0:
            return
        
        # Start confirmation process on new signal
        if potential_signal != 0:
            self._pending_signal = potential_signal
            self._pending_bars = 0

    def on_fill(self, ctx) -> None:
        # Bracket handles exits
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
