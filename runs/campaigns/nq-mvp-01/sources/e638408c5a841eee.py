"""
Mean-reversion fade with ultra-conservative confirmation and move magnitude focus.

Analysis of prior trials:
- #6: -0.3638 — best baseline, two-bar confirmation with body filtering
- #10: -0.1862 — progression requirement too strict
- #13: -0.3922 — reverted to #6 but added complexity
- #14: -0.3108 — adaptive bracket scaling didn't help

Key insight: #6 remains the best at -0.3638. The pattern is crystal clear:
- Simple two-bar confirmation works best
- Strict body filtering on both bars is essential
- Every attempt to add complexity or adaptive scaling makes it worse
- The issue with #14 was that bracket scaling by move_ratio diluted the signal

This strategy returns to #6's exact logic but with one crucial refinement:

1. Keep #6's proven core completely:
   - Two-bar pattern: outsized move + reversal with good body
   - No progression requirement, no conviction gates, no adaptive scaling
   - Strict body filtering (65% for move, 60% for confirmation)

2. Refine the outsized move detection:
   - Require even stricter range threshold (1.4 instead of 1.35)
   - Higher body ratio requirement (0.68 instead of 0.65)
   - This filters out marginal outsized moves, keeping only high-conviction ones

3. Revert bracket scaling to #6's fixed approach:
   - Stop offset: 0.75 (fixed, no scaling)
   - Target offset: 0.75 (fixed, no scaling)
   - This is what made #6 work - consistency without adaptation

4. Simpler size logic:
   - Base size 1, minimal scaling
   - Only scale size by move magnitude * 0.15 (less aggressive than #14's 0.2)
   - Cap at 1.5 contracts (more conservative)

5. Stricter session filtering:
   - Skip first 3 bars (more context needed)
   - Skip if < 15 minutes left (more conservative exit window)
   - Skip if typical_range < 0.12 (filter out dead markets)

The philosophy: #6 is the best performer. The pattern is that every
modification that tries to be "smarter" makes it worse. This proposal
tightens the filters on what counts as a valid setup (stricter outsized
detection) but keeps the bracket and sizing logic as close to #6 as possible.
The key is that fixed brackets work better than adaptive ones.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.40,
        min_body_ratio: float = 0.68,
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
        
        # Avoid trading in first 3 bars (need more session context)
        if self._bars_in_session < 4:
            return
        
        # Check time remaining - need at least 15 minutes
        time_left_ns = ctx.session_end - ctx.ts
        min_time_needed_ns = 15 * 60 * 1e9
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
        if typical_range < 0.12:
            return
        
        self._typical_range = typical_range
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # Strict outsized detection: range is large AND body is substantial
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
        
        # TWO-BAR CONFIRMATION (#6 logic)
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
        
        # Conservative size scaling - only scale slightly by move magnitude
        size_multiplier = 1.0 + move_ratio * 0.15
        size = max(1, int(self.base_size * min(1.5, size_multiplier)))
        
        # Fixed brackets (#6 approach) - no scaling by move_ratio
        # This is what made #6 work - consistency
        stop_offset = self.stop_offset
        target_offset = self.target_offset
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
