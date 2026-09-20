"""
Mean-reversion fade with adaptive body filtering and conviction-based entry.

Analysis of prior trials:
- #6: -0.3638 — baseline two-bar confirmation with body filtering
- #10: -0.1862 — too strict progression requirement
- #11: -0.2189 — adaptive volatility but lost signal sharpness
- #12: -0.2377 — conviction threshold 0.25 still filtering too much

Key insight: #6's -0.3638 is still the best. The problem with subsequent 
attempts is that we're being too selective about which reversals to trade.

Looking at the pattern:
- #6 works with simpler, less filtered logic
- #10-12 all tried to be more selective and all scored worse
- The conviction threshold approach (0.25 in #12) still filters out valid trades

This strategy goes back to #6's core insight but makes targeted improvements:

1. Revert to #6's simpler confirmation logic:
   - Two-bar pattern: outsized move followed by reversal
   - Strict body quality filtering (both bars)
   - No progression requirement, no conviction threshold gating

2. Improve the outsized move detection:
   - Use adaptive body ratio based on range
   - Bars with smaller ranges need proportionally larger bodies
   - This catches real directional bars while filtering noise

3. Better sizing discipline:
   - Size scales with move magnitude (how extreme the outsized move was)
   - Larger moves = larger trades (more conviction from the market)
   - This improves capital allocation

4. Refined bracket levels:
   - Stop offset: 0.80 (between #6's 0.85 and #10's 0.50)
   - Target offset: 0.38 (between #6's 0.40 and #10's 0.35)
   - Scale both by move magnitude for adaptive risk/reward

5. Session timing:
   - Skip first 2 bars (minimal context needed)
   - Skip if < 12 minutes left (enough time for mean reversion)
   - This is less restrictive than #12's 15 minutes

6. Minimum range filter:
   - Skip if typical_range < 0.15 (very tight filter)
   - This keeps us in markets with actual movement potential

The key difference from #12: remove the conviction threshold that gates entries.
Instead, let the two-bar confirmation pattern itself be the filter. If previous
was outsized and current reversed with good body, trade it. Don't add another
gate that filters out valid trades.
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
        stop_offset: float = 0.80,
        target_offset: float = 0.38,
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
        
        # Check time remaining - need at least 12 minutes
        time_left_ns = ctx.session_end - ctx.ts
        min_time_needed_ns = 12 * 60 * 1e9
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
        if typical_range < 0.15:
            return
        
        self._typical_range = typical_range
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # Adaptive body ratio based on range size
        # Smaller ranges need proportionally larger bodies (relative to their range)
        if current_range > 0:
            adaptive_body_threshold = current_range * self.min_body_ratio
        else:
            adaptive_body_threshold = 0.0
        
        # Outsized detection: range is large AND body is substantial
        is_outsized = (
            current_range > typical_range * self.move_threshold and
            current_body > adaptive_body_threshold
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
        
        # TWO-BAR CONFIRMATION (back to #6 simplicity)
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
        # Larger moves relative to typical range = larger trades
        size_multiplier = 1.0 + move_ratio * 0.25
        size = max(1, int(self.base_size * min(2.5, size_multiplier)))
        
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
