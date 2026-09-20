"""
Mean-reversion fade with refined confirmation and adaptive bracket scaling.

Analysis of prior trials:
- #6: -0.3638 — best performer, pure two-bar confirmation
- #18: -0.4503 — exact #6 replica, slightly worse (parameter variation)
- #10-17: all worse than #6, despite various refinements

Key insight: #6's -0.3638 score is hard to beat because the two-bar confirmation
pattern is genuinely robust. However, looking at the recent runs, there's a
consistent pattern: #6 worked with specific parameters (lookback=8, threshold=1.4,
body_ratio=0.70 for outsized, 0.60 for confirmation).

The hypothesis: #6's success came from the specific parameter combination, not
just the logic. Recent trials (#17, #18) have used slightly different values
(1.35 threshold, lookback=10) and performed worse.

This strategy:

1. Return exactly to #6's parameters that worked:
   - lookback: 8 (not 10)
   - move_threshold: 1.4 (not 1.35)
   - min_body_ratio: 0.70 (not 0.65)
   - confirmation_body_ratio: 0.60 (exact match)

2. Keep #6's proven logic:
   - Two-bar confirmation: outsized move + reversal with body quality
   - Fixed brackets: 0.75 stop, 0.75 target
   - Size: 1 contract
   - Skip first 3 bars

3. Add one targeted refinement based on observation:
   - Scale bracket sizes slightly by typical range magnitude
   - When typical_range is high (volatile session), use tighter brackets (0.70)
   - When typical_range is low (quiet session), use looser brackets (0.80)
   - This keeps the signal pure but adapts bracket sizing to market conditions
   - The adaptation is conservative: only ±0.05 from the base 0.75

4. Minimal time-based gate:
   - Only skip if < 4 bars remain (very conservative)
   - This avoids being too restrictive late in the session

The philosophy: #6 scored -0.3638 with exact parameters. We're returning to
those exact parameters but adding a minimal, conservative bracket adaptation
that should help in extreme volatility without diluting the core signal.
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
        
        # Conservative: only skip if very few bars left
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
                self._enter_fade(ctx, "up")
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                self._enter_fade(ctx, "down")
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction

    def _enter_fade(self, ctx, outsized_direction: str) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0:
            return
        
        # Adaptive bracket scaling based on volatility
        # High volatility: tighter brackets (0.70)
        # Low volatility: looser brackets (0.80)
        # Baseline is 0.75
        vol_ratio = self._typical_range / 0.50  # normalize against a reference
        if vol_ratio > 2.0:
            # Very high volatility: tighten
            stop_offset = 0.70
            target_offset = 0.70
        elif vol_ratio < 0.5:
            # Very low volatility: loosen
            stop_offset = 0.80
            target_offset = 0.80
        else:
            # Normal: use base
            stop_offset = self.stop_offset
            target_offset = self.target_offset
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=self.base_size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=self.base_size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
