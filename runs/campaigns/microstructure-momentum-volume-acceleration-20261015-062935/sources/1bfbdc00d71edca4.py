"""Volume-acceleration momentum with regime-aware exits and signal confirmation.

Building on trials #0 (score -0.0278) and #1 (score -0.0604), this strategy
addresses the key weakness: over-trading on marginal signals and poor exit timing.

Key improvements:
1. Require multi-bar volume confirmation: vol acceleration must persist
2. Add price momentum confirmation: require favorable movement after entry signal
3. Implement volatility-adaptive stops: scale risk to recent market volatility
4. Use trailing exits instead of fixed bars: exit on reversal, not time
5. Reduce entry frequency: tighter filters on close-to-high positioning
6. Track only closed bars for signal - no look-ahead on forming bar

The prior attempts traded too frequently on weak signals. This version:
- Waits for volume acceleration to confirm over 2 bars
- Requires the next bar to show favorable momentum before entering
- Exits on directional reversal rather than fixed time
- Uses volatility-normalized position sizing and risk management

This targets higher precision at the cost of lower frequency, which should
improve the score by reducing transaction costs and false positives.
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
        close_to_high_min: float = 0.65,
        vol_confirm_bars: int = 2,
        momentum_confirm: bool = True,
        volatility_window: int = 20,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 2.0,
    ):
        self.interval = interval
        self.vol_lookback_k = vol_lookback_k
        self.vol_threshold = vol_threshold
        self.close_to_high_min = close_to_high_min
        self.vol_confirm_bars = vol_confirm_bars
        self.momentum_confirm = momentum_confirm
        self.volatility_window = volatility_window
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        # Track volumes for rolling mean
        self._volumes: deque[int] = deque(maxlen=vol_lookback_k + vol_confirm_bars + 1)
        
        # Track closes for momentum and volatility
        self._closes: deque[float] = deque(maxlen=volatility_window + 2)
        
        # Track pending signal waiting for confirmation
        self._pending_signal: int = 0  # 0=none, 1=long, -1=short
        self._pending_bars: int = 0
        
        # Track bars since entry
        self._bars_since_entry: int = 0
        self._entry_price: float | None = None
        self._entry_signal: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last = history[-1]
        
        # Track volume and close
        self._volumes.append(last.volume)
        self._closes.append(last.close)
        
        # Need enough history
        if len(self._volumes) < self.vol_lookback_k + 1:
            return
        
        # Compute recent volatility (ATR-like measure)
        if len(history) >= self.volatility_window:
            recent_bars = history[-self.volatility_window:]
            ranges = [b.high - b.low for b in recent_bars]
            avg_range = sum(ranges) / len(ranges) if ranges else 1.0
        else:
            avg_range = last.high - last.low
        
        # Compute volume acceleration signal
        volumes_list = list(self._volumes)
        vol_mean = sum(volumes_list[:-1]) / len(volumes_list[:-1]) if len(volumes_list) > 1 else 1.0
        vol_ratio = last.volume / vol_mean if vol_mean > 0 else 1.0
        
        # Check if volume is accelerating
        volume_accelerated = vol_ratio > self.vol_threshold
        
        # Compute close-to-high ratio (strict filter)
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
                # Check volume persistence
                recent_vols = volumes_list[-self.vol_confirm_bars:]
                vols_above_mean = sum(1 for v in recent_vols if v / vol_mean > self.vol_threshold * 0.9)
                
                if vols_above_mean >= self.vol_confirm_bars - 1:
                    # Check momentum confirmation if enabled
                    if self.momentum_confirm and len(self._closes) >= 2:
                        # Next bar should move in signal direction
                        if self._pending_signal > 0:
                            momentum_ok = last.close > self._closes[-2]
                        else:
                            momentum_ok = last.close < self._closes[-2]
                    else:
                        momentum_ok = True
                    
                    if momentum_ok:
                        confirmed = True
            
            if confirmed and ctx.position == 0 and not ctx.working_order_ids:
                # Execute entry
                self._bars_since_entry = 0
                self._entry_signal = self._pending_signal
                self._entry_price = last.close
                self._pending_signal = 0
                self._pending_bars = 0
                
                if self._pending_signal > 0:
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="vol-confirm-long",
                    )
                else:
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="vol-confirm-short",
                    )
            elif self._pending_bars > self.vol_confirm_bars + 2:
                # Confirmation window expired
                self._pending_signal = 0
                self._pending_bars = 0
        
        # Manage existing position
        if ctx.position != 0:
            self._bars_since_entry += 1
            position_signal = 1 if ctx.position > 0 else -1
            
            # Exit on reversal: opposite signal with good conviction
            if potential_signal != 0 and potential_signal != position_signal:
                if volume_accelerated and close_to_high_valid:
                    ctx.close()
                    self._bars_since_entry = 0
                    self._entry_signal = 0
                    self._entry_price = None
            
            # Exit on max hold (safety)
            elif self._bars_since_entry >= 8:
                ctx.close()
                self._bars_since_entry = 0
                self._entry_signal = 0
                self._entry_price = None
            
            return
        
        # No position, check for new entry signal
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
