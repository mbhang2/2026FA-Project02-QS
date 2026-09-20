"""
Mean-reversion with three-bar confirmation and session-phase awareness.

Analysis of prior trials:
- #5: -0.5817 — two-bar confirmation works well
- #6: -0.3638 — best so far, body-quality filters improve entry precision

Key insight from #6: Stricter body-quality filtering on confirmation bar works
(-0.3638 vs -0.5817). The next refinement should:

1. Three-bar confirmation: Add one more bar to reduce false reversals
   - Bar N: outsized move (e.g., up)
   - Bar N+1: initial reversal (e.g., down)
   - Bar N+2: confirm reversal holds (doesn't reverse back up)
   This triple confirmation should catch only the strongest, most reliable fades.

2. Session-phase awareness: Adjust parameters based on time in session
   - Early session (bars 3-20): More aggressive, lower thresholds
   - Mid session (bars 21-60): Standard thresholds
   - Late session (bars 61+): Very conservative, tighter brackets, smaller size
   This trades more when conviction is easier to build, exits quickly when time is short.

3. Volatility adaptation: Scale bracket sizing to recent volatility
   - Use the typical range as a volatility proxy
   - Tighter stops/targets when range is low (easier to hit)
   - Wider stops/targets when range is high (more room needed)

4. Entry quality: Require confirmation bar to be in direction of fade
   - Not just opposite direction, but actually closing in fade direction
   - This ensures the reversal is real, not just a doji or indecision

The hypothesis: #6's body-quality filter is good, but adding a third confirmation
bar and session-phase awareness will further reduce false signals while improving
timing and risk management. Early-session aggressiveness captures more setups,
late-session conservatism avoids being caught.
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
        # Track three bars for three-bar confirmation
        self._bar_minus_2_outsized: bool = False
        self._bar_minus_2_direction: str | None = None
        self._bar_minus_1_reversal: bool = False
        self._bar_minus_1_direction: str | None = None
        self._typical_range: float = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _get_session_phase(self, bars_in_session: int) -> str:
        """Determine session phase for adaptive parameters."""
        if bars_in_session < 21:
            return "early"
        elif bars_in_session < 61:
            return "mid"
        else:
            return "late"

    def _get_phase_multipliers(self, phase: str) -> tuple[float, float, float]:
        """Get threshold and sizing multipliers for session phase.
        
        Returns: (threshold_mult, size_mult, bracket_mult)
        """
        if phase == "early":
            # Early: more aggressive, catch more setups
            return (0.95, 1.2, 1.0)
        elif phase == "mid":
            # Mid: standard
            return (1.0, 1.0, 1.0)
        else:
            # Late: conservative, quick exits
            return (1.1, 0.7, 0.7)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Reset on new session
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_in_session = 0
            self._bar_minus_2_outsized = False
            self._bar_minus_2_direction = None
            self._bar_minus_1_reversal = False
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
        
        # Get session phase for adaptive parameters
        phase = self._get_session_phase(self._bars_in_session)
        threshold_mult, size_mult, bracket_mult = self._get_phase_multipliers(phase)
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # Adjusted threshold based on phase
        adjusted_threshold = self.move_threshold * threshold_mult
        
        # Outsized detection: range is large AND body is substantial
        is_outsized = (
            current_range > typical_range * adjusted_threshold and
            current_body > current_range * self.min_body_ratio
        )
        
        current_direction = None
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        
        # THREE-BAR CONFIRMATION LOGIC
        # Bar N-2: outsized move (e.g., up)
        # Bar N-1: initial reversal (e.g., down with good body)
        # Bar N: confirm reversal holds (doesn't reverse back, moves further in fade direction)
        
        current_moved_up = current_bar.close > current_bar.open
        current_moved_down = current_bar.close < current_bar.open
        
        if (self._bar_minus_2_outsized and self._bar_minus_2_direction is not None and
            self._bar_minus_1_reversal and self._bar_minus_1_direction is not None):
            
            # Check if current bar confirms the reversal (doesn't reverse back)
            if (self._bar_minus_2_direction == "up" and self._bar_minus_1_direction == "down" and
                current_moved_down):
                # Outsized up, then down, now down again - strong fade signal
                self._enter_fade(ctx, "up", size_mult, bracket_mult)
            elif (self._bar_minus_2_direction == "down" and self._bar_minus_1_direction == "up" and
                  current_moved_up):
                # Outsized down, then up, now up again - strong fade signal
                self._enter_fade(ctx, "down", size_mult, bracket_mult)
        
        # Update bar N-1 reversal status for next iteration
        if self._bar_minus_2_outsized and self._bar_minus_2_direction is not None:
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # Check body quality of potential confirmation bar
            has_good_body = (
                current_body > current_range * self.confirmation_body_ratio
                if current_range > 0 else False
            )
            
            # Check for reversal
            if self._bar_minus_2_direction == "up" and current_moved_down and has_good_body:
                self._bar_minus_1_reversal = True
                self._bar_minus_1_direction = "down"
            elif self._bar_minus_2_direction == "down" and current_moved_up and has_good_body:
                self._bar_minus_1_reversal = True
                self._bar_minus_1_direction = "up"
            else:
                self._bar_minus_1_reversal = False
                self._bar_minus_1_direction = None
        else:
            self._bar_minus_1_reversal = False
            self._bar_minus_1_direction = None
        
        # Store current bar info for next iteration
        self._bar_minus_2_outsized = is_outsized
        self._bar_minus_2_direction = current_direction

    def _enter_fade(self, ctx, outsized_direction: str, size_mult: float, bracket_mult: float) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0:
            return
        
        # Conservative sizing with phase multiplier
        size_multiplier = min(2.0, 1.0 + 0.4) * size_mult
        size = max(1, int(self.base_size * size_multiplier))
        
        # Adaptive bracket sizing with phase multiplier
        stop_mult = self.stop_offset * bracket_mult
        target_mult = self.target_offset * bracket_mult * 0.9
        
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
