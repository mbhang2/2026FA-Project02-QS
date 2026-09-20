"""
Refined mean-reversion strategy with better timing and session context.

Previous trials:
- #0 (continuation): -0.7271
- #1 (mean-reversion fade): -0.6313
- #2 (multi-bar confirmation): crashed on WindowTooShort

Analysis of #2 crash: accessing history[-2] when history window is bounded causes
issues. The crash happened because we tried to look back further than the window
allows. Need to be more careful with history access.

This proposal refines further:
1. Simpler confirmation logic that doesn't reach back into history
2. Focus on the magnitude and persistence of outsized moves
3. Better sizing: scale based on how extreme the move is
4. Cleaner entry timing: enter on the bar after outsized move shows up
5. Use session time context to avoid late-session trades
6. Track state more carefully to avoid window access issues

Key insight from #1 vs #0: mean-reversion DOES work (beat continuation by ~0.1),
so the hypothesis is sound. The issue is timing and confirmation. We should:
- Keep it simple to avoid crashes
- Focus on high-conviction outsized moves (>1.5x typical)
- Size proportional to extremeness of the move
- Let the bracket handle exits (proven to work)
- Avoid complex multi-bar lookbacks that risk window errors
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 6,
        move_threshold: float = 1.5,
        base_size: int = 1,
        stop_offset: float = 1.2,
        target_offset: float = 0.6,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._last_outsized_direction: str | None = None
        self._last_outsized_magnitude: float = 0.0
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
            self._last_outsized_direction = None
            self._last_outsized_magnitude = 0.0
            self._typical_range = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        last_bar = history[-1]
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Avoid trading in first bar of session (no context)
        if self._bars_in_session < 2:
            return
        
        # Avoid trading very close to session end (not enough time for reversion)
        time_left_ns = ctx.session_end - ctx.ts
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)  # rough for 5m bars
        if bars_left_estimate < 2:
            return
        
        # Need enough history to measure typical range
        if len(history) < self.lookback + 1:
            return
        
        # Measure typical range from lookback window (excluding current bar)
        # Use safe slicing: get last lookback bars before current
        window_start = max(0, len(history) - self.lookback - 1)
        window_end = len(history) - 1  # exclude current bar
        
        if window_end - window_start < self.lookback:
            return
        
        window = history[window_start:window_end]
        typical_range = sum(b.high - b.low for b in window) / len(window) if len(window) > 0 else 0.0
        if typical_range <= 0:
            return
        
        self._typical_range = typical_range
        
        # Detect outsized move in current bar
        last_range = last_bar.high - last_bar.low
        last_move = abs(last_bar.close - last_bar.open)
        
        # Check if this bar is outsized
        is_outsized = (
            last_range > typical_range * self.move_threshold or
            last_move > typical_range * self.move_threshold
        )
        
        # If we had an outsized move last bar, this is our chance to fade it
        if self._last_outsized_direction is not None:
            # Determine if this bar shows signs of reversion
            # Simple check: does it move in opposite direction from outsized move?
            bar_moved_up = last_bar.close > last_bar.open
            
            if self._last_outsized_direction == "up" and not bar_moved_up:
                # Previous was up, current is down/flat - good reversion signal
                self._enter_fade(ctx, "up")
            elif self._last_outsized_direction == "down" and bar_moved_up:
                # Previous was down, current is up/flat - good reversion signal
                self._enter_fade(ctx, "down")
        
        # Store current bar's outsized info for next bar
        if is_outsized:
            if last_bar.close > last_bar.open:
                self._last_outsized_direction = "up"
                self._last_outsized_magnitude = last_bar.close - last_bar.open
            elif last_bar.close < last_bar.open:
                self._last_outsized_direction = "down"
                self._last_outsized_magnitude = last_bar.open - last_bar.close
            else:
                self._last_outsized_direction = None
                self._last_outsized_magnitude = 0.0
        else:
            self._last_outsized_direction = None
            self._last_outsized_magnitude = 0.0

    def _enter_fade(self, ctx, outsized_direction: str) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0:
            return
        
        # Size based on magnitude of outsized move
        size_multiplier = min(
            2.5,
            1.0 + (self._last_outsized_magnitude / self._typical_range) * 0.6
        )
        size = max(1, int(self.base_size * size_multiplier))
        
        # Scale bracket levels based on move magnitude
        move_ratio = self._last_outsized_magnitude / self._typical_range
        stop_mult = self.stop_offset * max(1.0, move_ratio)
        target_mult = self.target_offset * max(0.5, move_ratio * 0.8)
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=max(0.5, stop_mult),
                target_offset=max(0.25, target_mult),
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=max(0.5, stop_mult),
                target_offset=max(0.25, target_mult),
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
