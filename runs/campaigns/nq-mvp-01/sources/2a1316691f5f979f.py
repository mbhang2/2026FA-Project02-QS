"""
Mean-reversion fade with three-bar confirmation and tighter entry filtering.

Analysis of recent trials:
- #29: -0.1707 — best recent, strict body ratio (0.75) on outsized moves
- #30: -0.2675 — adaptive body ratio hurt performance, back to fixed approach
- #6: -0.3638 — all-time champion, but simpler with less selectivity

Key insight from the data: The two-bar pattern works, but #29 at -0.1707 suggests
we're still not optimal. Looking at the trials:
- Simpler strategies (#6) trade more but score worse
- More selective strategies (#29, #27, #28) trade less but score better
- The sweet spot seems to be around 130-150 trades per session

New hypothesis: The issue with #29 might be that we're catching reversals that
don't have staying power. A THREE-BAR confirmation would be more robust:
1. Bar N: Outsized move (strict body ratio 0.75)
2. Bar N+1: Reversal begins (opposite direction, good body)
3. Bar N+2: Reversal confirms (continues opposite direction, closes beyond N+1)

This adds a confirmation layer that filters out false reversals while keeping
the core mean-reversion logic. The three-bar pattern is more selective but
should have higher conviction trades.

Additional refinements:
- Tighter outsized move threshold (1.35 instead of 1.40) to catch more moves
- Higher confirmation body ratio (0.65) to ensure real reversals
- Slightly higher reversal strength threshold (0.18) for conviction
- Track the previous TWO bars for the three-bar pattern
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.35,
        min_body_ratio: float = 0.75,
        confirmation_body_ratio: float = 0.65,
        base_size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        reversal_strength_threshold: float = 0.18,
        min_typical_range: float = 0.10,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.reversal_strength_threshold = reversal_strength_threshold
        self.min_typical_range = min_typical_range
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        
        # Track bars for three-bar pattern
        self._bar_minus_2_outsized: bool = False
        self._bar_minus_2_direction: str | None = None
        self._bar_minus_2_open: float = 0.0
        
        self._bar_minus_1_is_reversal: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._bar_minus_1_close: float = 0.0
        
        self._typical_range: float = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Reset on new session
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_in_session = 0
            self._bar_minus_2_outsized = False
            self._bar_minus_2_direction = None
            self._bar_minus_2_open = 0.0
            self._bar_minus_1_is_reversal = False
            self._bar_minus_1_direction = None
            self._bar_minus_1_open = 0.0
            self._bar_minus_1_close = 0.0
            self._typical_range = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Avoid trading in first 5 bars (need session context)
        if self._bars_in_session < 6:
            return
        
        # Avoid trading in last 4 bars (need time for mean reversion)
        time_left_ns = ctx.session_end - ctx.ts
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)
        if bars_left_estimate < 4.0:
            return
        
        # Need enough history to measure typical range
        if len(history) < self.lookback + 1:
            return
        
        # Measure typical range from lookback window (excluding current bar)
        window_start = max(0, len(history) - self.lookback - 1)
        window_end = len(history) - 1
        
        if window_end - window_start < self.lookback:
            return
        
        window = history[window_start:window_end]
        if len(window) == 0:
            return
        
        typical_range = sum(b.high - b.low for b in window) / len(window)
        if typical_range < self.min_typical_range:
            return
        
        self._typical_range = typical_range
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # Outsized detection: range is large AND body is substantial
        is_outsized = (
            current_range > typical_range * self.move_threshold and
            current_body > current_range * self.min_body_ratio
        )
        
        current_direction = None
        current_open = current_bar.open
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        
        # THREE-BAR CONFIRMATION PATTERN
        # Bar N-2: Outsized move
        # Bar N-1: Reversal begins (opposite direction, good body)
        # Bar N (current): Reversal continues (closes beyond N-1's open)
        
        if (self._bar_minus_2_outsized and 
            self._bar_minus_2_direction is not None and
            self._bar_minus_1_is_reversal and
            self._bar_minus_1_direction is not None):
            
            # Check if current bar continues the reversal
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            
            # Reversal must be opposite to the original outsized move
            if (self._bar_minus_2_direction == "up" and 
                self._bar_minus_1_direction == "down" and
                current_moved_down):
                # Original was up, bar-1 reversed down, current continues down
                # Check if current closes beyond bar-1's open (progression)
                if current_bar.close < self._bar_minus_1_open:
                    # Measure reversal strength from the outsized move
                    reversal_distance = self._bar_minus_2_open - current_bar.close
                    reversal_range = self._bar_minus_2_open - current_bar.low if current_bar.low < self._bar_minus_2_open else 0.1
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        ctx.sell(
                            size=self.base_size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="fade-up-3bar",
                        )
            
            elif (self._bar_minus_2_direction == "down" and 
                  self._bar_minus_1_direction == "up" and
                  current_moved_up):
                # Original was down, bar-1 reversed up, current continues up
                # Check if current closes beyond bar-1's open (progression)
                if current_bar.close > self._bar_minus_1_open:
                    # Measure reversal strength from the outsized move
                    reversal_distance = current_bar.close - self._bar_minus_2_open
                    reversal_range = current_bar.high - self._bar_minus_2_open if current_bar.high > self._bar_minus_2_open else 0.1
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        ctx.buy(
                            size=self.base_size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="fade-down-3bar",
                        )
        
        # Check if current bar is a reversal (for next iteration's bar N+1 slot)
        is_reversal = False
        reversal_direction = None
        
        if self._bar_minus_2_outsized and self._bar_minus_2_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            has_good_body = (
                current_body > current_range * self.confirmation_body_ratio
                if current_range > 0 else False
            )
            
            # Reversal is opposite to the outsized move and has good body
            if self._bar_minus_2_direction == "up" and current_moved_down and has_good_body:
                is_reversal = True
                reversal_direction = "down"
            elif self._bar_minus_2_direction == "down" and current_moved_up and has_good_body:
                is_reversal = True
                reversal_direction = "up"
        
        # Shift the bars for next iteration
        self._bar_minus_2_outsized = self._bar_minus_1_is_reversal
        self._bar_minus_2_direction = self._bar_minus_1_direction
        self._bar_minus_2_open = self._bar_minus_1_open
        
        self._bar_minus_1_is_reversal = is_reversal
        self._bar_minus_1_direction = reversal_direction
        self._bar_minus_1_open = current_bar.open
        self._bar_minus_1_close = current_bar.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
