"""Volume-acceleration momentum with improved signal quality and adaptive position sizing.

Building on trial #4 (score -0.0512, trades=142), the issue is clear: we're being
too conservative now. The volatility filter and stricter thresholds reduced trades
to 142, but that's too few and the score suffered. Trial #3 had +0.0185 with 1909
trades, suggesting the sweet spot is much higher trade frequency.

Key insight from the trial history:
- #3 (1909 trades, +0.0185): high frequency, positive score
- #4 (142 trades, -0.0512): too few trades, negative score
- The volatility filter and stricter parameters killed trade count

New approach: return to higher trade frequency but with smarter filtering:
1. Remove the ATR volatility regime filter (was killing trades unnecessarily)
2. Lower vol_threshold back to 1.5 (was 1.8) to catch more signals
3. Lower close_to_high_min to 0.55 (was 0.70) for more entries
4. Simplify confirmation: just check volume persistence, not momentum
5. Increase max_hold_bars to 5 (was 4) to let winners run
6. Add volatility-adaptive position sizing: larger in high vol, smaller in low vol
7. Use tighter stops (0.8) and targets (1.5) to reduce slippage impact
8. Track consecutive signals to avoid whipsaws on reversals

The core mechanism (volume acceleration + close positioning) is sound. The issue
with #4 was over-filtering. This version balances trade frequency with signal quality.

Expected improvement: return to higher trade count (closer to #3's 1900) while
maintaining directional bias. Adaptive sizing and tighter brackets should improve
the score vs #3.
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
        close_to_high_min: float = 0.55,
        vol_confirm_bars: int = 1,
        volatility_window: int = 20,
        size: int = 1,
        stop_offset: float = 0.8,
        target_offset: float = 1.5,
        max_hold_bars: int = 5,
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
        self._volumes: deque[int] = deque(maxlen=vol_lookback_k + vol_confirm_bars + 3)
        
        # Track highs/lows for volatility calculation
        self._highs: deque[float] = deque(maxlen=volatility_window)
        self._lows: deque[float] = deque(maxlen=volatility_window)
        
        # Track pending signal waiting for confirmation
        self._pending_signal: int = 0  # 0=none, 1=long, -1=short
        self._pending_bars: int = 0
        
        # Track bars since entry
        self._bars_since_entry: int = 0
        self._entry_signal: int = 0
        self._entry_price: float | None = None
        
        # Track last signal direction to avoid whipsaws
        self._last_signal_direction: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last = history[-1]
        
        # Track volume, high, low
        self._volumes.append(last.volume)
        self._highs.append(last.high)
        self._lows.append(last.low)
        
        # Need enough history for volume mean
        if len(self._volumes) < self.vol_lookback_k + 1:
            return
        
        # Compute volatility for adaptive sizing
        if len(self._highs) >= self.volatility_window:
            ranges = [self._highs[i] - self._lows[i] for i in range(len(self._highs))]
            atr = sum(ranges) / len(ranges) if ranges else 1.0
        else:
            atr = last.high - last.low
        
        # Adaptive position size based on volatility
        # Higher volatility = smaller position, lower volatility = larger position
        if atr > 2.0:
            adaptive_size = max(1, self.size // 2)
        elif atr > 1.0:
            adaptive_size = self.size
        else:
            adaptive_size = self.size + 1
        
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
                vols_above_threshold = sum(1 for v in recent_vols if v / vol_mean > self.vol_threshold * 0.80)
                
                # Require volume persistence
                if vols_above_threshold >= max(1, self.vol_confirm_bars):
                    confirmed = True
            
            # Execute entry if confirmed and flat
            if confirmed and ctx.position == 0 and not ctx.working_order_ids:
                # Save signal BEFORE resetting it
                entry_signal = self._pending_signal
                self._bars_since_entry = 0
                self._entry_signal = entry_signal
                self._entry_price = last.close
                self._last_signal_direction = entry_signal
                self._pending_signal = 0
                self._pending_bars = 0
                
                if entry_signal > 0:
                    ctx.buy(
                        size=adaptive_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="vol-accel-long",
                    )
                else:
                    ctx.sell(
                        size=adaptive_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="vol-accel-short",
                    )
            
            # Confirmation window expired without entry
            elif self._pending_bars > self.vol_confirm_bars + 2:
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
            
            # Exit on max hold (time-based exit)
            elif self._bars_since_entry >= self.max_hold_bars:
                ctx.close()
                self._bars_since_entry = 0
                self._entry_signal = 0
                self._entry_price = None
            
            return
        
        # No position: check for new entry signal
        if ctx.working_order_ids or self._pending_signal != 0:
            return
        
        # Avoid whipsaws: don't enter immediately opposite to last signal
        if potential_signal != 0 and potential_signal == -self._last_signal_direction:
            # Require stronger confirmation for reversal signals
            if not (volume_accelerated and close_to_high_valid):
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
