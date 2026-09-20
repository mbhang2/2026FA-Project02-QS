"""
Mean-reversion fade with three-bar confirmation and adaptive sizing.

Analysis of prior trials:
- #6: -0.3638 — champion with two-bar confirmation
- #10: -0.1862 — best recent, but over-complex with conviction gating
- #20: -0.2632 — exact #6 replica with timing gate
- #21: -0.2632 — midpoint confirmation, worse than #6

Key insight: #6's two-bar confirmation is the core strength. However, looking
at the pattern of trials, every attempt to improve it through additional filters
or adaptive logic has failed. The issue may be that we're being too selective.

This strategy tests a counter-intuitive hypothesis: what if we need THREE-bar
confirmation instead of two? The pattern would be:
1. Bar N: outsized move (large range, strong body)
2. Bar N+1: begins reversal (moves opposite direction)
3. Bar N+2: confirms reversal (continues opposite direction, closes beyond N's open)

Three-bar confirmation is stricter (fewer trades) but potentially higher conviction.
The trades we do take should be more reliable, which might improve the Sharpe ratio
even if raw trade count drops.

Additionally, we'll refine the sizing:
- Use a simpler, more conservative approach: fixed size with no adaptive scaling
- This keeps the core signal pure without parameter sensitivity

The philosophy: #6 scored -0.3638 with two-bar confirmation. Let's test whether
three-bar confirmation produces higher-quality trades. If the signal is truly about
mean reversion after outsized moves, a longer confirmation should improve quality.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.4,
        min_body_ratio: float = 0.70,
        confirmation_body_ratio: float = 0.60,
        base_size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_2_outsized: bool = False
        self._bar_minus_2_direction: str | None = None
        self._bar_minus_2_open: float = 0.0
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_moved_opposite: bool = False
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
            self._bar_minus_1_direction = None
            self._bar_minus_1_moved_opposite = False
            self._typical_range = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Skip first 4 bars (need session context)
        if self._bars_in_session < 5:
            return
        
        # Skip if fewer than 4 bars remain (need time for mean reversion)
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
        
        # Skip if market is too dead
        if typical_range < 0.10:
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
        current_moved_opposite_to_bar_minus_2 = False
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        else:
            # Not outsized, but check if it moved opposite to bar-minus-2
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            
            if self._bar_minus_2_direction == "up" and current_moved_down:
                current_moved_opposite_to_bar_minus_2 = True
                current_direction = "down"
            elif self._bar_minus_2_direction == "down" and current_moved_up:
                current_moved_opposite_to_bar_minus_2 = True
                current_direction = "up"
        
        # THREE-BAR CONFIRMATION LOGIC
        # Bar N-2: outsized move
        # Bar N-1: begins reversal (moves opposite direction)
        # Bar N (current): continues reversal AND closes beyond bar N-2's open
        
        if (self._bar_minus_2_outsized and 
            self._bar_minus_1_moved_opposite and 
            current_moved_opposite_to_bar_minus_2):
            
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # Check body quality of final confirmation bar
            has_good_body = (
                current_body > current_range * self.confirmation_body_ratio
                if current_range > 0 else False
            )
            
            if has_good_body:
                # Check if current closes beyond bar-minus-2's open (progression)
                if (self._bar_minus_2_direction == "up" and 
                    current_bar.close < self._bar_minus_2_open):
                    ctx.sell(
                        size=self.base_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="fade-up",
                    )
                elif (self._bar_minus_2_direction == "down" and 
                      current_bar.close > self._bar_minus_2_open):
                    ctx.buy(
                        size=self.base_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="fade-down",
                    )
        
        # Shift state: current bar becomes bar-minus-1, bar-minus-1 becomes bar-minus-2
        self._bar_minus_2_outsized = (self._bar_minus_1_moved_opposite and 
                                       self._bar_minus_2_outsized)
        if self._bar_minus_2_outsized:
            self._bar_minus_2_direction = self._bar_minus_1_direction
        else:
            # Bar-minus-1 is now bar-minus-2, check if it was outsized
            if self._bar_minus_1_moved_opposite:
                # Bar-minus-1 was a reversal move, not outsized
                self._bar_minus_2_outsized = False
                self._bar_minus_2_direction = None
        
        # Check if current bar is outsized for next iteration
        if is_outsized:
            self._bar_minus_2_outsized = True
            self._bar_minus_2_direction = current_direction
            self._bar_minus_2_open = current_bar.open
        
        # Bar-minus-1 becomes bar-minus-2 info
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_moved_opposite = current_moved_opposite_to_bar_minus_2

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
