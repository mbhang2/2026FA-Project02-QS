"""Volume-acceleration momentum with improved signal filtering and exit timing.

Building on trial #5 (score -0.0540, trades=538), the recent history shows:
- #3: +0.0185 with 1909 trades (high frequency, positive)
- #4: -0.0512 with 142 trades (too few, negative)
- #5: -0.0540 with 538 trades (middle ground, but still negative)

The pattern is clear: we need to be selective about which signals we trade, not
just trade more frequently. The issue with #5 was that we lowered thresholds too
much, catching weak signals that don't have real directional follow-through.

Key insights from the research hypothesis:
- Volume acceleration + close-to-high positioning should identify real momentum
- But we're entering on too many marginal signals
- Exit discipline is critical: we need to exit on actual reversals, not just time

New approach:
1. Return to moderate thresholds (vol_threshold=1.6, close_to_high=0.58)
2. Stricter confirmation: require TWO consecutive bars showing signal persistence
3. Add price momentum filter: next bar must close in signal direction
4. Smarter exit logic: track entry momentum and exit on clear reversal, not just time
5. Reduce max_hold from 5 to 3 bars: exit stale positions faster
6. Use slightly wider stops (1.0) but tighter targets (1.6) for better risk/reward
7. Track signal strength (how strong was the volume acceleration) and only enter on strong signals
8. Add a simple momentum oscillator: (close - close[3 bars ago]) to confirm direction

The core insight: we should be more selective about entries (fewer false signals)
and more aggressive about exits (don't hold losers). This balances trade frequency
with signal quality.

Expected improvement: fewer trades than #3 but higher win rate, better exit timing
than #5, and positive score by being more selective about which momentum signals
we actually trade.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        vol_lookback_k: int = 10,
        vol_threshold: float = 1.6,
        close_to_high_min: float = 0.58,
        vol_confirm_bars: int = 2,
        momentum_lookback: int = 3,
        size: int = 1,
        stop_offset: float = 1.0,
        target_offset: float = 1.6,
        max_hold_bars: int = 3,
    ):
        self.interval = interval
        self.vol_lookback_k = vol_lookback_k
        self.vol_threshold = vol_threshold
        self.close_to_high_min = close_to_high_min
        self.vol_confirm_bars = vol_confirm_bars
        self.momentum_lookback = momentum_lookback
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.max_hold_bars = max_hold_bars
        
        # Track volumes for rolling mean
        self._volumes: deque[int] = deque(maxlen=vol_lookback_k + vol_confirm_bars + 3)
        
        # Track closes for momentum calculation
        self._closes: deque[float] = deque(maxlen=momentum_lookback + 2)
        
        # Track opens for directional signal
        self._opens: deque[float] = deque(maxlen=momentum_lookback + 2)
        
        # Track highs/lows
        self._highs: deque[float] = deque(maxlen=vol_lookback_k + 5)
        self._lows: deque[float] = deque(maxlen=vol_lookback_k + 5)
        
        # Track pending signal waiting for confirmation
        self._pending_signal: int = 0  # 0=none, 1=long, -1=short
        self._pending_bars: int = 0
        self._pending_signal_strength: float = 0.0
        
        # Track bars since entry
        self._bars_since_entry: int = 0
        self._entry_signal: int = 0
        self._entry_price: float | None = None
        self._entry_momentum: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last = history[-1]
        
        # Track volumes, closes, opens, highs, lows
        self._volumes.append(last.volume)
        self._closes.append(last.close)
        self._opens.append(last.open)
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
        signal_strength = max(0, (vol_ratio - self.vol_threshold) / self.vol_threshold)
        
        # Compute close-to-high ratio
        bar_range = last.high - last.low
        if bar_range > 0:
            close_to_high = (last.close - last.low) / bar_range
        else:
            close_to_high = 0.5
        
        close_to_high_valid = close_to_high > self.close_to_high_min
        
        # Directional signal from current bar
        price_direction = 1 if last.close > last.open else (-1 if last.close < last.open else 0)
        
        # Calculate momentum: change over last N bars
        momentum = 0.0
        if len(self._closes) > self.momentum_lookback:
            momentum = self._closes[-1] - self._closes[-(self.momentum_lookback + 1)]
        
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
                vols_above_threshold = sum(1 for v in recent_vols if v / vol_mean > self.vol_threshold * 0.75)
                
                # Require volume persistence and price momentum confirmation
                if vols_above_threshold >= max(1, self.vol_confirm_bars - 1):
                    # Check momentum confirmation: price should move in signal direction
                    if len(self._closes) >= 2:
                        if self._pending_signal > 0:
                            momentum_ok = last.close > self._closes[-2]
                        else:
                            momentum_ok = last.close < self._closes[-2]
                    else:
                        momentum_ok = True
                    
                    if momentum_ok:
                        confirmed = True
            
            # Execute entry if confirmed and flat
            if confirmed and ctx.position == 0 and not ctx.working_order_ids:
                # Save signal BEFORE resetting it
                entry_signal = self._pending_signal
                self._bars_since_entry = 0
                self._entry_signal = entry_signal
                self._entry_price = last.close
                self._entry_momentum = momentum
                self._pending_signal = 0
                self._pending_bars = 0
                self._pending_signal_strength = 0.0
                
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
            elif self._pending_bars > self.vol_confirm_bars + 2:
                self._pending_signal = 0
                self._pending_bars = 0
                self._pending_signal_strength = 0.0
        
        # Manage existing position
        if ctx.position != 0:
            self._bars_since_entry += 1
            position_signal = 1 if ctx.position > 0 else -1
            
            # Exit on clear reversal: opposite signal with volume and positioning
            if potential_signal != 0 and potential_signal != position_signal:
                if volume_accelerated and close_to_high_valid:
                    ctx.close()
                    self._bars_since_entry = 0
                    self._entry_signal = 0
                    self._entry_price = None
                    self._entry_momentum = None
                    return
            
            # Exit on momentum reversal: momentum changed sign significantly
            if self._entry_momentum is not None and len(self._closes) > self.momentum_lookback:
                current_momentum = self._closes[-1] - self._closes[-(self.momentum_lookback + 1)]
                if (self._entry_momentum > 0 and current_momentum < 0) or \
                   (self._entry_momentum < 0 and current_momentum > 0):
                    ctx.close()
                    self._bars_since_entry = 0
                    self._entry_signal = 0
                    self._entry_price = None
                    self._entry_momentum = None
                    return
            
            # Exit on max hold (aggressive time-based exit)
            if self._bars_since_entry >= self.max_hold_bars:
                ctx.close()
                self._bars_since_entry = 0
                self._entry_signal = 0
                self._entry_price = None
                self._entry_momentum = None
            
            return
        
        # No position: check for new entry signal
        if ctx.working_order_ids or self._pending_signal != 0:
            return
        
        # Start confirmation process on new signal, but only if signal is strong
        if potential_signal != 0 and signal_strength > 0.2:
            self._pending_signal = potential_signal
            self._pending_bars = 0
            self._pending_signal_strength = signal_strength

    def on_fill(self, ctx) -> None:
        # Bracket handles exits
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
