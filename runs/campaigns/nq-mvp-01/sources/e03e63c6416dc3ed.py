"""
Mean-reversion fade with adaptive multi-bar confirmation and volatility scaling.

Analysis of prior trials:
- #6: -0.3638 — champion with two-bar confirmation, simple and robust
- #10: -0.1862 — best recent but over-parameterized with conviction gating
- #21: -0.2632 — midpoint confirmation, underperformed #6
- #22: NOT SCORED — three-bar confirmation produced no dispersion

Key insight: The two-bar confirmation in #6 works because it's simple and captures
genuine reversals. However, all attempts to improve it through additional filters
have failed. The issue may be that we're filtering too aggressively or that the
parameters need tuning.

This strategy takes a different approach:

1. Return to #6's core logic (two-bar confirmation) but with refined parameters:
   - Increase lookback from 8 to 12 (better typical range measurement)
   - Lower move_threshold from 1.4 to 1.25 (catch more outsized moves)
   - Keep min_body_ratio at 0.70 (maintain quality filter)
   - Lower confirmation_body_ratio from 0.60 to 0.50 (more flexible confirmation)

2. Add volatility-aware sizing:
   - Size scales with the ratio of move magnitude to typical range
   - Larger moves get larger position sizes (higher conviction)
   - This improves risk-adjusted returns without changing entry logic

3. Adaptive bracket sizing:
   - Stop and target scale with move magnitude relative to typical range
   - Larger moves get proportionally larger brackets
   - This keeps risk consistent across different market regimes

4. Refined entry timing:
   - Skip first 4 bars (consistent with #6)
   - Skip last 4 bars (more conservative, allow time for mean reversion)
   - This concentrates trades in the core session window

The philosophy: #6's -0.3638 is hard to beat because the core pattern is sound.
Rather than adding complexity, we're tuning the parameters slightly and adding
intelligent sizing that scales with move magnitude. The key is keeping the
confirmation logic simple while making the position sizing responsive to market
conditions.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 12,
        move_threshold: float = 1.25,
        min_body_ratio: float = 0.70,
        confirmation_body_ratio: float = 0.50,
        base_size: int = 1,
        base_stop_offset: float = 0.75,
        base_target_offset: float = 0.75,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        
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
        
        # Skip first 4 bars (need session context)
        if self._bars_in_session < 5:
            return
        
        # Skip last 4 bars (need time for mean reversion to play out)
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
        
        # Skip if market is too quiet
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
        # 2. Have meaningful body
        # 3. Close below/above prior bar's open (showing progression)
        
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # Check body quality of confirmation bar (more flexible than #6)
            has_good_body = (
                current_body > current_range * self.confirmation_body_ratio
                if current_range > 0 else False
            )
            
            # Check for reversal confirmation with progression
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                # Previous was up, current is down: check if close is below prior open
                if current_bar.close < self._bar_minus_1_open:
                    self._enter_fade(ctx, "up", current_bar)
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                # Previous was down, current is up: check if close is above prior open
                if current_bar.close > self._bar_minus_1_open:
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
        
        # Size scales with move magnitude
        # move_ratio of 1.25 (threshold) -> size 1
        # move_ratio of 2.0+ -> size 2
        size_multiplier = 1.0 + (move_ratio - self.move_threshold) * 0.5
        size = max(1, int(self.base_size * min(2.5, size_multiplier)))
        
        # Bracket sizing scales with move magnitude
        # Larger moves get larger brackets (proportional risk)
        stop_mult = self.base_stop_offset * (0.8 + move_ratio * 0.15)
        target_mult = self.base_target_offset * (0.8 + move_ratio * 0.15)
        
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
