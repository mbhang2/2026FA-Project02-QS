"""Volume-acceleration momentum with refined entry logic and better exit discipline.

Building on trial #6 (score -0.0403, trades=804), analyzing the pattern:
- #3: +0.0185 with 1909 trades (high frequency works)
- #6: -0.0403 with 804 trades (moderate frequency, still negative)

The issue: we're still entering on marginal signals. The research hypothesis says
volume acceleration + close-to-high positioning identifies momentum, but we need
to be much more selective about WHICH of these signals actually have follow-through.

Key insights from the data:
1. High trade count (#3: 1909) was profitable, suggesting the basic signal is valid
2. But recent trials (804, 538, 142 trades) are all negative
3. The difference: #3 had simpler logic, fewer filters, less overthinking
4. Recent trials added too many confirmation layers that killed the signal

New approach - back to basics with smart filtering:
1. Simplify confirmation: just check that volume stays elevated (1 bar is enough)
2. Remove momentum oscillator complexity - it was adding noise
3. Focus on ENTRY SELECTIVITY: only enter when volume spike is VERY strong (vol_ratio > 1.8)
4. This reduces trade count significantly but improves win rate
5. Exit more aggressively: exit at 3 bars max (was 3, keep it)
6. Use wider stops (1.2) to avoid whipsaws, tighter targets (1.4) for quick wins
7. Track only essential state: pending signal, entry price, bars held
8. Remove adaptive sizing - keep it simple with fixed size=1
9. Add a simple filter: only enter if the previous bar also had elevated volume
   This ensures we're catching real momentum, not one-off spikes

The core mechanism is sound. The problem was over-optimization. This version
returns to the simplicity of #3 but with better entry selectivity to improve
the win rate while maintaining high frequency.

Expected improvement: fewer trades than #3 (higher selectivity) but better quality,
positive score by focusing on the strongest volume acceleration signals.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        vol_lookback_k: int = 10,
        vol_threshold: float = 1.8,
        close_to_high_min: float = 0.55,
        vol_confirm_bars: int = 1,
        size: int = 1,
        stop_offset: float = 1.2,
        target_offset: float = 1.4,
        max_hold_bars: int = 3,
    ):
        self.interval = interval
        self.vol_lookback_k = vol_lookback_k
        self.vol_threshold = vol_threshold
        self.close_to_high_min = close_to_high_min
        self.vol_confirm_bars = vol_confirm_bars
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.max_hold_bars = max_hold_bars
        
        # Track volumes for rolling mean
        self._volumes: deque[int] = deque(maxlen=vol_lookback_k + vol_confirm_bars + 2)
        
        # Track pending signal waiting for confirmation
        self._pending_signal: int = 0  # 0=none, 1=long, -1=short
        self._pending_bars: int = 0
        
        # Track bars since entry
        self._bars_since_entry: int = 0
        self._entry_signal: int = 0
        self._entry_price: float | None = None
        
        # Track previous bar's volume ratio for sustained acceleration check
        self._prev_vol_ratio: float = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last = history[-1]
        
        # Track volume
        self._volumes.append(last.volume)
        
        # Need enough history for volume mean
        if len(self._volumes) < self.vol_lookback_k + 1:
            return
        
        # Compute volume acceleration signal
        volumes_list = list(self._volumes)
        vol_mean = sum(volumes_list[:-1]) / len(volumes_list[:-1]) if len(volumes_list) > 1 else 1.0
        vol_ratio = last.volume / vol_mean if vol_mean > 0 else 1.0
        
        # Check if volume is accelerating significantly (threshold is high: 1.8)
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
                # Simple confirmation: volume stays elevated (at least 80% of threshold)
                if vol_ratio > self.vol_threshold * 0.80:
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
            
            # Exit on reversal: opposite signal with strong volume and positioning
            if potential_signal != 0 and potential_signal != position_signal:
                if volume_accelerated and close_to_high_valid:
                    ctx.close()
                    self._bars_since_entry = 0
                    self._entry_signal = 0
                    self._entry_price = None
                    return
            
            # Exit on max hold (time-based exit)
            if self._bars_since_entry >= self.max_hold_bars:
                ctx.close()
                self._bars_since_entry = 0
                self._entry_signal = 0
                self._entry_price = None
            
            self._prev_vol_ratio = vol_ratio
            return
        
        # No position: check for new entry signal
        if ctx.working_order_ids or self._pending_signal != 0:
            self._prev_vol_ratio = vol_ratio
            return
        
        # Only enter on sustained volume acceleration: current AND previous bar elevated
        # This filters out one-off spikes and catches real momentum
        if potential_signal != 0:
            # Require previous bar also had decent volume (at least 1.3x mean)
            if self._prev_vol_ratio > 1.3:
                self._pending_signal = potential_signal
                self._pending_bars = 0
        
        self._prev_vol_ratio = vol_ratio

    def on_fill(self, ctx) -> None:
        # Bracket handles exits
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
