"""
Mean-reversion with refined entry filters and adaptive exit scaling.

Analysis of prior trials:
- #0 (continuation): -0.7271 — continuation hypothesis loses money
- #1 (mean-reversion): -0.6313 — mean-reversion works but overshoots entries
- #3 (baseline): -0.6232 — solid one-bar confirmation approach
- #5 (best): -0.5817 — two-bar confirmation reduces noise, improves quality

Key insight from #5: Two-bar confirmation works well (-0.5817 vs -0.6232).
The next refinement should focus on:

1. Entry quality: Make confirmation even stricter
   - Require the confirmation bar to have meaningful body (not just direction)
   - Filter for high-conviction reversals only
   
2. Exit optimization: Current bracket sizing may be too aggressive
   - Tighter targets to lock in profits more reliably
   - Dynamic stops based on session context
   
3. Sizing discipline: More conservative on uncertain setups
   - Reduce size when confirmation is weaker
   - Scale more aggressively only on strongest signals
   
4. Session awareness: Trade more actively early, taper late
   - Early session: more setups, higher conviction threshold
   - Late session: very selective, quick exits

The hypothesis: #5's confirmation is good but not selective enough. Adding
body-size requirements to the confirmation bar will filter out weak reversals
while keeping the strong mean-reversion trades that work.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.4,
        min_body_ratio: float = 0.65,
        confirmation_body_ratio: float = 0.55,
        base_size: int = 1,
        stop_offset: float = 0.85,
        target_offset: float = 0.40,
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
        
        # Avoid trading in first 2 bars of session (no context)
        if self._bars_in_session < 3:
            return
        
        # Avoid trading very close to session end
        time_left_ns = ctx.session_end - ctx.ts
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)
        if bars_left_estimate < 2.5:
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
        if typical_range <= 0:
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
        
        # IMPROVED TWO-BAR CONFIRMATION LOGIC
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body (not just a wick)
        # This filters out weak reversals and noise
        
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
            
            # Check for reversal confirmation with good body
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                # Previous was up, current is down with conviction - fade the up move
                self._enter_fade(ctx, "up")
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                # Previous was down, current is up with conviction - fade the down move
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
        
        # Conservative sizing: scale based on move magnitude
        # Smaller moves: base size, larger moves: up to 2x
        size_multiplier = min(2.0, 1.0 + move_ratio * 0.4)
        size = max(1, int(self.base_size * size_multiplier))
        
        # Adaptive bracket sizing
        # Tighter on moderate moves, wider on extreme moves
        stop_mult = self.stop_offset * move_ratio
        target_mult = self.target_offset * move_ratio * 0.9
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=max(0.5, stop_mult),
                target_offset=max(0.2, target_mult),
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=max(0.5, stop_mult),
                target_offset=max(0.2, target_mult),
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
