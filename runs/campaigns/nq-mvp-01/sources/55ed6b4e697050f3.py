"""
Mean-reversion with adaptive sizing and multi-timeframe confirmation.

Previous trials analysis:
- #0 (continuation): -0.7271 — continuation hypothesis loses money
- #1 (mean-reversion fade): -0.6313 — mean-reversion WORKS, beats continuation by ~0.1
- #2 (multi-bar confirmation): crashed on WindowTooShort — accessing history[-2] is risky
- #3 (current best): -0.6232 — simplified approach, avoids window issues

Key insights:
1. Mean-reversion is the RIGHT direction (fade outsized moves)
2. The issue is NOT the hypothesis, it's execution details
3. Entry timing matters: entering right after outsized move is better than waiting
4. Sizing matters: scale based on move magnitude
5. Window safety: never assume history depth beyond what we check

This proposal refines #3 with:
1. Tighter move thresholds (1.3 instead of 1.5) — catch more signals
2. Better sizing: use move magnitude ratio more aggressively
3. Adaptive target offset: scale target based on session volatility
4. More selective entry: require higher confidence (both range AND body size)
5. Longer lookback (8 instead of 6): better typical range estimate
6. Track session volatility evolution to adapt bracket sizing
7. Only enter when session is active (not first/last bars)

The core insight: #1 proved mean-reversion works. #3 showed safe implementation.
Now we optimize the parameters and execution details to improve from -0.6232.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.3,
        min_body_ratio: float = 0.6,
        base_size: int = 1,
        stop_offset: float = 1.0,
        target_offset: float = 0.5,
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
        self._last_outsized_direction: str | None = None
        self._last_outsized_magnitude: float = 0.0
        self._typical_range: float = 0.0
        self._session_volatility: float = 1.0

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
            self._session_volatility = 1.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        last_bar = history[-1]
        
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
        typical_range = sum(b.high - b.low for b in window) / len(window) if len(window) > 0 else 0.0
        if typical_range <= 0:
            return
        
        self._typical_range = typical_range
        
        # Update session volatility: running average of all bar ranges so far
        if self._bars_in_session > 1:
            all_ranges = sum(b.high - b.low for b in history)
            session_avg_range = all_ranges / len(history)
            self._session_volatility = session_avg_range / typical_range if typical_range > 0 else 1.0
        
        # Detect outsized move in current bar
        last_range = last_bar.high - last_bar.low
        last_body = abs(last_bar.close - last_bar.open)
        
        # Outsized detection: range is large AND body is substantial
        # This filters out wide bars with small bodies (less conviction)
        is_outsized = (
            last_range > typical_range * self.move_threshold and
            last_body > last_range * self.min_body_ratio
        )
        
        # If we had an outsized move last bar, check for fade opportunity this bar
        if self._last_outsized_direction is not None:
            bar_moved_up = last_bar.close > last_bar.open
            
            # Fade: enter opposite to the outsized move direction
            if self._last_outsized_direction == "up" and not bar_moved_up:
                self._enter_fade(ctx, "up")
            elif self._last_outsized_direction == "down" and bar_moved_up:
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
        
        # Size based on magnitude of outsized move relative to typical range
        move_ratio = self._last_outsized_magnitude / self._typical_range
        size_multiplier = min(2.5, 1.0 + move_ratio * 0.8)
        size = max(1, int(self.base_size * size_multiplier))
        
        # Scale bracket levels based on:
        # 1. How extreme the move was (move_ratio)
        # 2. Overall session volatility (self._session_volatility)
        move_ratio = self._last_outsized_magnitude / self._typical_range
        vol_factor = max(0.8, min(1.5, self._session_volatility))
        
        # Stop: tighter on low-volatility sessions, wider on high-volatility
        stop_mult = self.stop_offset * move_ratio * vol_factor
        
        # Target: aggressive on extreme moves, conservative on moderate ones
        target_mult = self.target_offset * max(0.6, move_ratio * 0.7)
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=max(0.4, stop_mult),
                target_offset=max(0.2, target_mult),
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=max(0.4, stop_mult),
                target_offset=max(0.2, target_mult),
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
