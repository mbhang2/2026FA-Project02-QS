"""
Mean-reversion fade with ultra-simple two-bar pattern and minimal parameterization.

Analysis of prior trials:
- #6: -0.3638 — best performer, pure two-bar confirmation
- #10-17: all worse than #6, despite various refinements

Key insight: Every modification to #6 has made performance worse. The pattern is clear:
- Two-bar confirmation works (outsized move + reversal with body quality)
- Fixed brackets work (0.75 stop, 0.75 target)
- Minimal parameterization works
- Session position awareness, adaptive scaling, conviction gating all hurt

This strategy strips away everything added after #6 and returns to the core:

1. Exact #6 logic:
   - Outsized move: range > 1.35x typical, body > 65% of range
   - Reversal confirmation: opposite direction, body > 60% of range
   - No progression requirement, no conviction gates, no adaptive logic

2. Simplify everything else:
   - Skip first 3 bars only (proven from #6)
   - No complex time-based gates or bar-counting
   - Fixed lookback of 8 bars (what #6 used)
   - Minimum range threshold of 0.10
   - Fixed size of 1 contract
   - Fixed brackets: 0.75 stop, 0.75 target

3. Remove all the complexity that failed:
   - No session position awareness
   - No adaptive sizing by move magnitude
   - No conviction scaling
   - No late-session reduction
   - No progression beyond reversal

The philosophy: #6 scored -0.3638 with pure, simple logic. Every trial since
has tried to improve it and failed. This proposal is #6 with only the minimal
necessary improvements to avoid edge cases - nothing more.

The key observation: in the results, the simplest approaches consistently
outperform complex ones. #6 remains unbeaten. Occam's Razor applies.
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
            self._typical_range = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Avoid trading in first 3 bars (need session context)
        if self._bars_in_session < 4:
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
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        
        # TWO-BAR CONFIRMATION (exactly #6 logic)
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
                ctx.sell(
                    size=self.base_size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="fade-up",
                )
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                ctx.buy(
                    size=self.base_size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="fade-down",
                )
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
