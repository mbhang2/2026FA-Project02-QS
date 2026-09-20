"""
Mean-reversion fade with simplified signal and adaptive bracket scaling.

Analysis of prior trials:
- #6: -0.3638 — two-bar confirmation baseline, very clean
- #10: -0.1862 — progression requirement too strict
- #12: -0.2377 — conviction threshold 0.25 filters too much
- #13: -0.3922 — reverted to #6 but added complexity that hurt

Key insight: #6's -0.3638 is still the best. The pattern is clear:
- Simple two-bar confirmation works
- Strict body filtering on both bars works
- Sizing based on move magnitude works
- Every attempt to add more filters or gates makes it worse

This strategy strips back to #6's core but with one targeted improvement:

1. Keep #6's proven logic exactly:
   - Two-bar pattern: outsized move + reversal with good body
   - No progression requirement, no conviction gates
   - Strict body filtering on both bars

2. Fix the bracket scaling issue:
   - #6 used fixed offsets (0.75 stop, 0.75 target)
   - This doesn't adapt to market conditions
   - Scale brackets by move magnitude but keep it simple
   - Larger moves get proportionally larger brackets

3. Cleaner session timing:
   - Skip first 2 bars (minimal context)
   - Skip if < 10 minutes left (conservative but not excessive)
   - This is less restrictive than #12's 15 minutes

4. Simpler size logic:
   - Base size 1, scale by move magnitude
   - move_ratio = outsized_magnitude / typical_range
   - size = base_size * (1 + move_ratio * 0.2)
   - Cap at 2 contracts to avoid oversizing

5. Better typical range calculation:
   - Use last N bars excluding current (same as before)
   - But require minimum range of 0.10 (less strict than #13's 0.15)
   - This lets us trade in slightly quieter markets

The core philosophy: #6 works because it's simple and focused. Every 
subsequent trial that tried to improve it by adding gates/thresholds made 
it worse. This proposal goes back to #6's simplicity but scales the brackets 
adaptively to the move magnitude, which is a natural improvement without 
adding complexity.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.35,
        min_body_ratio: float = 0.65,
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
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_magnitude: float = 0.0
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
            self._bar_minus_1_outsized = False
            self._bar_minus_1_direction = None
            self._bar_minus_1_magnitude = 0.0
            self._typical_range = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Avoid trading in first 2 bars (need some session context)
        if self._bars_in_session < 3:
            return
        
        # Check time remaining - need at least 10 minutes
        time_left_ns = ctx.session_end - ctx.ts
        min_time_needed_ns = 10 * 60 * 1e9
        if time_left_ns < min_time_needed_ns:
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
        current_magnitude = 0.0
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
                current_magnitude = current_bar.close - current_bar.open
            elif current_bar.close < current_bar.open:
                current_direction = "down"
                current_magnitude = current_bar.open - current_bar.close
        
        # TWO-BAR CONFIRMATION (back to #6 logic)
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body quality
        
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # Check body quality of confirmation bar
            has_good_body = (
                current_body > current_range * self.confirmation_body_ratio
                if current_range > 0 else False
            )
            
            # Check for reversal confirmation
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                self._enter_fade(ctx, "up")
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                self._enter_fade(ctx, "down")
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_magnitude = current_magnitude

    def _enter_fade(self, ctx, outsized_direction: str) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0 or self._bar_minus_1_magnitude <= 0:
            return
        
        # Measure how extreme the outsized move was
        move_ratio = self._bar_minus_1_magnitude / self._typical_range
        
        # Size based on move magnitude
        size_multiplier = 1.0 + move_ratio * 0.2
        size = max(1, int(self.base_size * min(2.0, size_multiplier)))
        
        # Bracket sizing: scale by move magnitude
        # Larger moves get proportionally larger brackets
        stop_mult = self.stop_offset * move_ratio
        target_mult = self.target_offset * move_ratio
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=max(0.50, stop_mult),
                target_offset=max(0.25, target_mult),
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=max(0.50, stop_mult),
                target_offset=max(0.25, target_mult),
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
