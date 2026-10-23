"""Volume-acceleration momentum with dynamic entry filtering and volatility-adjusted exits.

Analyzing the trial history:
- #3: +0.0185 with 1909 trades (high frequency, profitable)
- #7: -0.0116 with 213 trades (low frequency, slightly negative)
- #6: -0.0403 with 804 trades (medium frequency, negative)

The pattern is clear: high-frequency trading (#3) was profitable, but recent attempts
to reduce trade count and improve selectivity have failed. The issue is that we're
being TOO selective and missing the actual signal.

Key insight from #3's success:
- It had simple logic: volume acceleration + close positioning + direction
- It entered frequently (1909 trades) and was profitable
- Recent trials tried to reduce frequency but that degraded performance

New strategy - return to #3's core logic but with surgical improvements:

1. RESTORE HIGH FREQUENCY: Use lower vol_threshold (1.5) to catch more signals
   This matches #3's approach which worked well

2. BETTER ENTRY CONFIRMATION: Instead of requiring consecutive bars with volume,
   use a simpler check: just confirm the signal persists for 1 bar, but require
   the DIRECTION to continue (close moving in the signal direction)

3. DYNAMIC EXIT TIMING: Instead of fixed max_hold_bars (3), use:
   - Exit immediately if price reverses AND volume accelerates in opposite direction
   - Otherwise hold for 4-5 bars to capture the microstructure momentum window
   - This targets the 2-5 bar window mentioned in the research hypothesis

4. VOLATILITY-ADJUSTED STOPS: Track recent volatility and size stops accordingly
   - Higher volatility = wider stops (to avoid whipsaws)
   - Lower volatility = tighter stops (to protect capital)
   - This improves risk-adjusted returns

5. REDUCE OVERTHINKING: Remove the "previous bar volume" requirement that #7 added
   - It was too restrictive and killed profitable signals
   - Just use the core: vol acceleration + close positioning + direction

6. TIGHTER TARGETS: Use 1.3 target offset to take quick profits
   - Microstructure momentum is short-lived (2-5 bars)
   - Don't be greedy; take what the market gives

The core mechanism from #3 was sound. This version improves on it with better
exit discipline and volatility awareness, while maintaining the high frequency
that made #3 profitable.

Expected improvement: frequency similar to #3 (1500+ trades) with better exit
discipline, targeting a positive score by combining high frequency with smart exits.
"""

from __future__ import annotations
from collections import deque
import math


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
        stop_offset: float = 1.2,
        target_offset: float = 1.3,
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
        
        # Track closes for volatility calculation
        self._closes: deque[float] = deque(maxlen=volatility_window + 2)
        
        # Track highs/lows for range
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

    def _calculate_volatility(self) -> float:
        """Calculate recent volatility as ATR-like measure."""
        if len(self._highs) < 2:
            return 1.0
        
        ranges = [h - l for h, l in zip(self._highs, self._lows)]
        avg_range = sum(ranges) / len(ranges) if ranges else 1.0
        return max(0.5, avg_range)  # Minimum volatility floor

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
                # Confirmation: check that price continues in signal direction
                if len(self._closes) >= 2:
                    if self._pending_signal > 0:
                        price_continuing = last.close > self._closes[-2]
                    else:
                        price_continuing = last.close < self._closes[-2]
                else:
                    price_continuing = True
                
                # Also check volume is still elevated
                if price_continuing and vol_ratio > self.vol_threshold * 0.75:
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
                
                # Calculate volatility-adjusted stops
                recent_vol = self._calculate_volatility()
                vol_adjusted_stop = self.stop_offset * (recent_vol / 1.0) if recent_vol > 0 else self.stop_offset
                vol_adjusted_target = self.target_offset * (recent_vol / 1.0) if recent_vol > 0 else self.target_offset
                
                if entry_signal > 0:
                    ctx.buy(
                        size=self.size,
                        stop_offset=vol_adjusted_stop,
                        target_offset=vol_adjusted_target,
                        tag="vol-accel-long",
                    )
                else:
                    ctx.sell(
                        size=self.size,
                        stop_offset=vol_adjusted_stop,
                        target_offset=vol_adjusted_target,
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
            
            # Exit on clear reversal: opposite signal with volume and positioning
            if potential_signal != 0 and potential_signal != position_signal:
                if volume_accelerated and close_to_high_valid:
                    ctx.close()
                    self._bars_since_entry = 0
                    self._entry_signal = 0
                    self._entry_price = None
                    return
            
            # Exit on max hold - target the 2-5 bar microstructure window
            if self._bars_since_entry >= self.max_hold_bars:
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
