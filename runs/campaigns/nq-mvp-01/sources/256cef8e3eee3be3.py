"""
Mean-reversion fade with dynamic confirmation and conviction-based sizing.

Analysis of prior trials:
- #6: -0.3638 — two-bar confirmation with body-quality filtering (best baseline)
- #10: -0.1862 — too strict progression requirement (close beyond prior open)
- #11: -0.2189 — adaptive volatility but lost the sharp signal

Key insight: #6's simplicity and sharp filtering is the core strength. The 
issue with #10 was the progression requirement was too strict. The issue with 
#11 was relaxing it too much and adding complexity that diluted the signal.

This strategy refines the approach by:

1. Return to core #6 logic but with refinements:
   - Two-bar confirmation: previous bar outsized, current bar reverses
   - Strict body quality filtering on BOTH bars
   - This is the signal that works

2. Better conviction measurement:
   - Measure how far the confirmation bar closed in the fade direction
   - But use a more lenient threshold (0.25 instead of 0.3)
   - This recovers some trades #10 filtered out while keeping quality high

3. Tighter bar selection:
   - Skip first 2 bars (not 3) - need less session context
   - Skip last 3 bars only if time is truly short (< 15 min)
   - This trades more frequently while staying safe

4. Cleaner sizing:
   - Size based on move magnitude only, not conviction
   - Conviction only gates whether we trade (threshold), not size
   - Simpler logic = more predictable behavior

5. Better bracket discipline:
   - Stop offset: 0.75 (tighter than #6's 0.85, but not as tight as #10's 0.50)
   - Target offset: 0.35 (between #6 and #10)
   - Scale both by move magnitude (simple multiplier)

6. Minimum range filter:
   - Skip if typical_range < 0.2 (avoid dead markets)
   - This keeps us out of choppy, low-conviction environments
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
        conviction_threshold: float = 0.25,
        base_size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.35,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.conviction_threshold = conviction_threshold
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_magnitude: float = 0.0
        self._bar_minus_1_open: float = 0.0
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
            self._bar_minus_1_open = 0.0
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
        
        # Skip if market is too choppy or dead
        if typical_range < 0.2:
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
        current_open = current_bar.open
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
                current_magnitude = current_bar.close - current_bar.open
            elif current_bar.close < current_bar.open:
                current_direction = "down"
                current_magnitude = current_bar.open - current_bar.close
        
        # TWO-BAR CONFIRMATION
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
                self._enter_fade(ctx, "up", current_bar)
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                self._enter_fade(ctx, "down", current_bar)
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_magnitude = current_magnitude
        self._bar_minus_1_open = current_open

    def _enter_fade(self, ctx, outsized_direction: str, current_bar) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0 or self._bar_minus_1_magnitude <= 0:
            return
        
        # Measure how extreme the outsized move was
        move_ratio = self._bar_minus_1_magnitude / self._typical_range
        
        # Measure reversal conviction: how far current closed in fade direction
        if outsized_direction == "up":
            # For fade-up (sell), conviction is how far below prior open
            reversal_distance = self._bar_minus_1_open - current_bar.close
            reversal_range = self._bar_minus_1_open - current_bar.low if current_bar.low < self._bar_minus_1_open else 0.1
        else:
            # For fade-down (buy), conviction is how far above prior open
            reversal_distance = current_bar.close - self._bar_minus_1_open
            reversal_range = current_bar.high - self._bar_minus_1_open if current_bar.high > self._bar_minus_1_open else 0.1
        
        # Conviction score: 0.0 to 1.0
        conviction = min(1.0, max(0.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5))
        
        # Gate on conviction threshold (0.25 is lenient but still filters weak reversals)
        if conviction < self.conviction_threshold:
            return
        
        # Size based on move magnitude only (not conviction)
        # Conviction gates the trade, magnitude sizes it
        size_multiplier = 1.0 + move_ratio * 0.3
        size = max(1, int(self.base_size * min(2.0, size_multiplier)))
        
        # Bracket sizing: scale by move magnitude
        stop_mult = self.stop_offset * move_ratio
        target_mult = self.target_offset * move_ratio
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=max(0.50, stop_mult),
                target_offset=max(0.20, target_mult),
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=max(0.50, stop_mult),
                target_offset=max(0.20, target_mult),
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
