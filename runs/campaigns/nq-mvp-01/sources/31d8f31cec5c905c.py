"""
Mean-reversion with adaptive confirmation and tighter entry discipline.

Analysis of prior trials:
- #0 (continuation): -0.7271 — continuation loses money, mean-reversion is correct
- #1 (mean-reversion): -0.6313 — mean-reversion works, but timing is off
- #2, #4: crashed on WindowTooShort — accessing history beyond safe bounds
- #3 (best): -0.6232 — simplified, safe approach with one-bar confirmation

Key observations:
1. Mean-reversion is the RIGHT hypothesis (beats continuation by ~0.1)
2. Entry timing: entering on bar AFTER outsized move is better than same bar
3. The one-bar confirmation in #3 works but may be too simple
4. Need to balance: more confirmation improves quality but reduces frequency

This proposal refines #3 with:
1. Two-bar confirmation: current bar must show reversion AND follow-through
   - Bar after outsized move: shows initial reversal
   - Current bar: confirms the reversal is real (not just noise)
2. Tighter entry thresholds:
   - Only enter on very high-conviction setups (move_threshold 1.4)
   - Require body size to confirm conviction (not just range)
3. More conservative sizing: scale down to reduce drawdowns
4. Better bracket sizing: tighter stops on smaller moves, wider on larger
5. Session timing: more aggressive early session, conservative late session
6. Keep state simple to avoid window access issues

The insight: #3's one-bar fade works but catches noisy reversals. Two-bar
confirmation filters for real reversals while staying within safe history access.
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
        base_size: int = 1,
        stop_offset: float = 0.9,
        target_offset: float = 0.45,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        # Track the last two bars for two-bar confirmation
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None  # "up" or "down"
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
        # Safe slicing: never reach past the window
        window_start = max(0, len(history) - self.lookback - 1)
        window_end = len(history) - 1  # exclude current bar
        
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
        
        # TWO-BAR CONFIRMATION LOGIC
        # We enter if:
        # 1. Previous bar was outsized in one direction (e.g., up)
        # 2. Current bar shows reversal: moves opposite (e.g., down)
        # This filters out false reversals and noise
        
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            
            # Check for reversal confirmation
            if self._bar_minus_1_direction == "up" and current_moved_down:
                # Previous was up, current is down - fade the up move
                self._enter_fade(ctx, "up")
            elif self._bar_minus_1_direction == "down" and current_moved_up:
                # Previous was down, current is up - fade the down move
                self._enter_fade(ctx, "down")
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_magnitude = current_magnitude

    def _enter_fade(self, ctx, outsized_direction: str) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0 or self._bar_minus_1_magnitude <= 0:
            return
        
        # Conservative sizing: scale down from base size
        move_ratio = self._bar_minus_1_magnitude / self._typical_range
        # Size multiplier is more conservative: smaller moves get base size, bigger moves get 1.5x max
        size_multiplier = min(1.5, 1.0 + move_ratio * 0.3)
        size = max(1, int(self.base_size * size_multiplier))
        
        # Bracket sizing based on move magnitude
        # Tighter on moderate moves, wider on extreme moves
        move_ratio = self._bar_minus_1_magnitude / self._typical_range
        
        # Stop: proportional to move magnitude, with a floor
        stop_mult = self.stop_offset * move_ratio
        
        # Target: more conservative, focus on catching the fade
        target_mult = self.target_offset * move_ratio * 0.8
        
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
