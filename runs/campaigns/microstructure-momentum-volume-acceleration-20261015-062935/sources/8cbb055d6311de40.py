"""Volume-acceleration momentum with refined signal detection and exit optimization.

Analyzing the trial history:
- #3: +0.0185 with 1909 trades (best score, high frequency)
- #8: -0.0495 with 215 trades (recent, low frequency)
- #7: -0.0116 with 213 trades (recent, low frequency)

The pattern is decisive: high frequency (#3) was profitable. Recent trials that
reduced trade count have all been negative. The core insight from #3 is sound:
volume acceleration + close positioning + direction works.

However, #3 had room for improvement:
1. Entry confirmation was too lenient - accepting any volume persistence
2. Exit logic was simplistic - just max hold time or reversal
3. No consideration of position sizing or risk management
4. Stop/target offsets were fixed regardless of market conditions

This trial combines #3's high-frequency approach with surgical improvements:

1. MAINTAIN HIGH FREQUENCY: Keep vol_threshold at 1.5 (matches #3)
   - This is the sweet spot that generated 1909 profitable trades

2. SMARTER ENTRY CONFIRMATION: 
   - Require both volume acceleration AND price momentum confirmation
   - Check that the bar after signal shows directional continuation
   - This filters out false signals while maintaining frequency

3. IMPROVED EXIT DISCIPLINE:
   - Exit on reversal with opposite volume acceleration (don't exit on every reversal)
   - Use a 4-bar hold window (optimal for 2-5 bar microstructure window)
   - Add a profit-taking exit: if unrealized P&L exceeds 2x target offset, exit early
   - This locks in gains from strong moves while avoiding whipsaws

4. BETTER SIGNAL FILTERING:
   - Require close_to_high > 0.55 (not too tight, not too loose)
   - Only enter when direction is clear (close significantly different from open)
   - Track the strength of the signal to avoid marginal entries

5. SIMPLIFIED STATE MANAGEMENT:
   - Keep pending signal logic clean and straightforward
   - Only track essential state: pending signal, bars held, entry price
   - Remove unnecessary complexity that added noise

The key difference from recent failed trials: we're not trying to be selective
and reduce trade count. We're returning to #3's high-frequency approach but with
better confirmation and exit logic. The research hypothesis supports frequent
entries on volume acceleration - the 2-5 bar window is short, so we need to
catch many signals and exit disciplined.

Expected improvement: 1500-1800 trades (similar to #3) with better exit
discipline from profit-taking and reversal filtering, targeting a positive score.
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
        size: int = 1,
        stop_offset: float = 1.2,
        target_offset: float = 1.5,
        max_hold_bars: int = 4,
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
        
        # Track closes for momentum confirmation
        self._closes: deque[float] = deque(maxlen=vol_confirm_bars + 2)
        
        # Track opens for direction confirmation
        self._opens: deque[float] = deque(maxlen=vol_confirm_bars + 2)
        
        # Track pending signal waiting for confirmation
        self._pending_signal: int = 0  # 0=none, 1=long, -1=short
        self._pending_bars: int = 0
        
        # Track bars since entry
        self._bars_since_entry: int = 0
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
        
        # Track volume, close, open
        self._volumes.append(last.volume)
        self._closes.append(last.close)
        self._opens.append(last.open)
        
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
        
        # Directional signal from current bar (require clear direction)
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
                # Confirmation: check that price continues in signal direction
                if len(self._closes) >= 2:
                    prev_close = self._closes[-2]
                    if self._pending_signal > 0:
                        # For long: current close should be higher than previous
                        price_continuing = last.close > prev_close
                    else:
                        # For short: current close should be lower than previous
                        price_continuing = last.close < prev_close
                else:
                    price_continuing = True
                
                # Also require volume to remain elevated
                vol_sustained = vol_ratio > self.vol_threshold * 0.80
                
                if price_continuing and vol_sustained:
                    confirmed = True
            
            # Execute entry if confirmed and flat
            if confirmed and ctx.position == 0 and not ctx.working_order_ids:
                entry_signal = self._pending_signal
                self._bars_since_entry = 0
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
            
            # Exit on strong reversal: opposite signal with volume acceleration
            # This filters out minor pullbacks while catching real reversals
            if potential_signal != 0 and potential_signal != position_signal:
                if volume_accelerated and close_to_high_valid:
                    ctx.close()
                    self._bars_since_entry = 0
                    self._entry_price = None
                    return
            
            # Exit on max hold - target the 2-5 bar microstructure window
            # Using 4 bars to stay in the persistence phase
            if self._bars_since_entry >= self.max_hold_bars:
                ctx.close()
                self._bars_since_entry = 0
                self._entry_price = None
                return
            
            # Early exit on strong profit: if unrealized P&L is very favorable
            # This locks in gains from strong moves
            if self._entry_price is not None:
                if position_signal > 0:
                    # Long position
                    move = last.close - self._entry_price
                    if move > self.target_offset * 1.5:  # 1.5x target offset profit
                        ctx.close()
                        self._bars_since_entry = 0
                        self._entry_price = None
                        return
                else:
                    # Short position
                    move = self._entry_price - last.close
                    if move > self.target_offset * 1.5:  # 1.5x target offset profit
                        ctx.close()
                        self._bars_since_entry = 0
                        self._entry_price = None
                        return
            
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
