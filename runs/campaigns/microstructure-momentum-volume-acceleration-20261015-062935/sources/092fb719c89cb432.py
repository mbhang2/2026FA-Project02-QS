"""Volume-acceleration momentum with regime-aware signal filtering and adaptive exits.

Building on trial #3 (score +0.0185, trades=1909), this strategy addresses the
core insight: the high trade count (1909 trades over 1290 sessions) suggests we're
entering too frequently on marginal signals, causing transaction costs and slippage
to dominate.

Key improvements from #3:
1. Stricter volume acceleration threshold: require vol_ratio > 1.8 (was 1.5)
2. Higher close-to-high minimum: 0.70 (was 0.60) to filter weak positioning
3. Add momentum confirmation: next bar must show continuation, not just signal
4. Reduce confirmation bars: 1 bar (was 2) to balance signal strength vs entry speed
5. Shorter max hold: 4 bars (was 6) to exit stale positions faster
6. Add volatility filter: only trade when ATR is in normal range (not too quiet)
7. Require sustained volume: check that vol_ratio doesn't collapse immediately

The prior strategy had good directional bias but traded too much. This version:
- Raises the bar for entry signals (stricter volume and positioning)
- Adds momentum confirmation to reduce false entries
- Exits faster on reversal or time
- Filters out low-volatility regimes where microstructure effects are weaker

Expected improvement: fewer, higher-conviction trades with better exit timing
should reduce transaction costs and improve the score significantly.
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
        close_to_high_min: float = 0.70,
        vol_confirm_bars: int = 1,
        volatility_window: int = 20,
        min_atr_threshold: float = 0.3,
        max_atr_threshold: float = 5.0,
        size: int = 1,
        stop_offset: float = 1.2,
        target_offset: float = 1.8,
        max_hold_bars: int = 4,
    ):
        self.interval = interval
        self.vol_lookback_k = vol_lookback_k
        self.vol_threshold = vol_threshold
        self.close_to_high_min = close_to_high_min
        self.vol_confirm_bars = vol_confirm_bars
        self.volatility_window = volatility_window
        self.min_atr_threshold = min_atr_threshold
        self.max_atr_threshold = max_atr_threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.max_hold_bars = max_hold_bars
        
        # Track volumes for rolling mean
        self._volumes: deque[int] = deque(maxlen=vol_lookback_k + vol_confirm_bars + 3)
        
        # Track closes for momentum
        self._closes: deque[float] = deque(maxlen=volatility_window + 2)
        
        # Track highs/lows for volatility
        self._highs: deque[float] = deque(maxlen=volatility_window)
        self._lows: deque[float] = deque(maxlen=volatility_window)
        
        # Track pending signal waiting for confirmation
        self._pending_signal: int = 0  # 0=none, 1=long, -1=short
        self._pending_bars: int = 0
        self._pending_entry_price: float | None = None
        
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
        
        # Compute volatility (ATR-like)
        if len(self._highs) >= self.volatility_window:
            ranges = [self._highs[i] - self._lows[i] for i in range(len(self._highs))]
            atr = sum(ranges) / len(ranges) if ranges else 1.0
        else:
            atr = last.high - last.low
        
        # Filter out low and high volatility regimes
        if atr < self.min_atr_threshold or atr > self.max_atr_threshold:
            # Don't trade in extreme volatility regimes
            if self._pending_signal != 0:
                self._pending_signal = 0
                self._pending_bars = 0
            return
        
        # Compute volume acceleration signal
        volumes_list = list(self._volumes)
        vol_mean = sum(volumes_list[:-1]) / len(volumes_list[:-1]) if len(volumes_list) > 1 else 1.0
        vol_ratio = last.volume / vol_mean if vol_mean > 0 else 1.0
        
        # Check if volume is accelerating (stricter threshold)
        volume_accelerated = vol_ratio > self.vol_threshold
        
        # Compute close-to-high ratio (stricter filter)
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
                    # Check momentum confirmation: next bar should move in signal direction
                    if len(self._closes) >= 2:
                        if self._pending_signal > 0:
                            momentum_ok = last.close > self._pending_entry_price
                        else:
                            momentum_ok = last.close < self._pending_entry_price
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
                self._pending_signal = 0
                self._pending_bars = 0
                self._pending_entry_price = None
                
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
                self._pending_entry_price = None
        
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
            
            # Exit on max hold (safety valve, shorter now)
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
            self._pending_entry_price = last.close

    def on_fill(self, ctx) -> None:
        # Bracket handles exits
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
