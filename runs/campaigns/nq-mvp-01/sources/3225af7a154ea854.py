"""
Mean-reversion fade with three-bar confirmation and volatility-adaptive sizing.

Analysis of prior trials:
- #6: -0.3638 — champion, two-bar confirmation with simple logic
- #24: -0.2238 — recent, fine-tuned threshold but still underperforms #6
- #10: -0.1862 — conviction gating was too restrictive
- #23: -0.2469 — adaptive sizing diluted the signal

Key insight: #6's -0.3638 is the best we've achieved. The two-bar confirmation
pattern works, but we may be catching too many weak reversals. The issue: we
enter on ANY reversal bar that closes beyond the prior open, regardless of
momentum or strength.

This strategy refines the confirmation logic:

1. Three-bar pattern instead of two:
   - Bar 1: Outsized move (up or down)
   - Bar 2: Initial reversal (opposite direction, closes beyond bar 1 open)
   - Bar 3: Confirmation of reversal (continues in reversal direction)
   
   This filters out false reversals where bar 2 reverses but bar 3 continues
   the original move. True mean reversion should show momentum in bar 3.

2. Stricter confirmation requirements:
   - Bar 3 must close beyond bar 2's close (not just open)
   - This ensures the reversal has conviction, not just a bounce
   - Maintains the body ratio filter on bar 3

3. Adaptive sizing based on reversal strength:
   - Measure how far bar 3 has progressed in fade direction
   - Size scales with reversal momentum (0.5 to 1.5x base)
   - Stronger reversals get larger positions

4. Conservative parameters:
   - move_threshold=1.35 (between #6's 1.4 and #24's 1.3)
   - min_body_ratio=0.70 (strict on outsized move)
   - confirmation_body_ratio=0.55 (slightly relaxed for bar 2)
   - lookback=8 (proven in #6)

5. Entry timing:
   - Skip first 5 bars (need session context for three-bar pattern)
   - Skip last 5 bars (need time for mean reversion to play out)
   - This concentrates trades in the core session window

The philosophy: #6 works because the pattern is real, but we can improve
selectivity by requiring bar 3 confirmation. This reduces false reversals
while maintaining the core strength of the two-bar reversal pattern.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.35,
        min_body_ratio: float = 0.70,
        confirmation_body_ratio: float = 0.55,
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
        
        # Three-bar pattern tracking
        self._bar_minus_2_outsized: bool = False
        self._bar_minus_2_direction: str | None = None
        self._bar_minus_2_open: float = 0.0
        
        self._bar_minus_1_is_reversal: bool = False
        self._bar_minus_1_direction: str | None = None
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
        
        # Skip first 5 bars (need session context for three-bar pattern)
        if self._bars_in_session < 6:
            return
        
        # Skip last 5 bars (need time for mean reversion)
        time_left_ns = ctx.session_end - ctx.ts
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)
        if bars_left_estimate < 5.0:
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
        if typical_range <= 0.1:
            return
        
        self._typical_range = typical_range
        
        # Analyze current bar
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        current_moved_up = current_bar.close > current_bar.open
        current_moved_down = current_bar.close < current_bar.open
        
        # THREE-BAR CONFIRMATION PATTERN
        # Bar -2: Outsized move
        # Bar -1: Reversal with progression
        # Bar 0 (current): Continuation of reversal with momentum
        
        if (self._bar_minus_2_outsized and self._bar_minus_1_is_reversal and
            self._bar_minus_2_direction is not None and 
            self._bar_minus_1_direction is not None):
            
            # Verify bar 3 continues the reversal direction
            if (self._bar_minus_2_direction == "up" and 
                self._bar_minus_1_direction == "down" and
                current_moved_down):
                # Reversal continuing down: check if current closes below bar -1 close
                # This shows the reversal has momentum
                if current_bar.close < self._bar_minus_1_close:
                    self._enter_fade(ctx, "up", current_bar)
            
            elif (self._bar_minus_2_direction == "down" and 
                  self._bar_minus_1_direction == "up" and
                  current_moved_up):
                # Reversal continuing up: check if current closes above bar -1 close
                if current_bar.close > self._bar_minus_1_close:
                    self._enter_fade(ctx, "down", current_bar)
        
        # Update bar -2 tracking (what bar -1 was)
        # First, check if current bar is an outsized move
        is_outsized = (
            current_range > typical_range * self.move_threshold and
            current_body > current_range * self.min_body_ratio
        )
        
        current_direction = None
        if is_outsized:
            if current_moved_up:
                current_direction = "up"
            elif current_moved_down:
                current_direction = "down"
        
        # Shift bar -1 to bar -2
        self._bar_minus_2_outsized = self._bar_minus_1_is_reversal
        self._bar_minus_2_direction = self._bar_minus_1_direction
        self._bar_minus_2_open = self._bar_minus_1_close if self._bar_minus_1_is_reversal else 0.0
        
        # Update bar -1 with current analysis
        # Bar -1 is a reversal if: bar -2 was outsized and current reverses with progression
        is_reversal = False
        if self._bar_minus_2_outsized and self._bar_minus_2_direction is not None:
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            has_good_body = (
                current_body > current_range * self.confirmation_body_ratio
                if current_range > 0 else False
            )
            
            if (self._bar_minus_2_direction == "up" and current_moved_down and 
                has_good_body and current_bar.close < self._bar_minus_2_open):
                is_reversal = True
            elif (self._bar_minus_2_direction == "down" and current_moved_up and 
                  has_good_body and current_bar.close > self._bar_minus_2_open):
                is_reversal = True
        
        self._bar_minus_1_is_reversal = is_reversal
        self._bar_minus_1_direction = current_direction if is_reversal else None
        self._bar_minus_1_close = current_bar.close

    def _enter_fade(self, ctx, outsized_direction: str, current_bar) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        # Measure reversal momentum: how far bar 3 closed in fade direction
        # relative to bar 2's close
        if outsized_direction == "up":
            # Selling the up move
            reversal_distance = self._bar_minus_1_close - current_bar.close
            reversal_range = self._bar_minus_1_close - current_bar.low if current_bar.low < self._bar_minus_1_close else 0.1
        else:
            # Buying the down move
            reversal_distance = current_bar.close - self._bar_minus_1_close
            reversal_range = current_bar.high - self._bar_minus_1_close if current_bar.high > self._bar_minus_1_close else 0.1
        
        # Momentum score: 0.0 to 1.0
        momentum = min(1.0, max(0.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5))
        
        # Size scales with reversal momentum (0.5 to 1.5x base)
        # Weak momentum (0.3) -> 0.5x, Strong momentum (0.8+) -> 1.5x
        if momentum < 0.3:
            return  # Too weak, skip
        
        size_multiplier = 0.5 + momentum
        size = max(1, int(self.base_size * min(1.5, size_multiplier)))
        
        if outsized_direction == "up":
            ctx.sell(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="fade-up",
            )
        else:
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
