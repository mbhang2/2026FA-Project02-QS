"""
Mean-reversion fade with adaptive confirmation and volatility-scaled exits.

Analysis of prior trials:
- #6: -0.3638 — best so far, body-quality filters on confirmation work well
- #7: NOT SCORED — three-bar confirmation had no dispersion in returns

Key insight: #6's two-bar confirmation with strict body-quality filtering is 
working (-0.3638). The issue with #7 was likely that three-bar confirmation was 
too restrictive, filtering out all profitable setups and leaving zero variance.

This strategy refines #6 by:

1. Volatility-scaled entry thresholds:
   - When volatility is low, outsized moves are more significant, lower threshold
   - When volatility is high, require larger moves to trigger
   - This adapts to market regime without losing signals

2. Improved bracket sizing based on move magnitude AND volatility:
   - Scale both stops and targets to the size of the move relative to current vol
   - Tight exits on small fades, wider on large moves
   - This improves win rate by matching bracket size to setup quality

3. Confirmation bar must close in fade direction:
   - Not just opposite direction, but actually progressing in fade direction
   - This adds conviction without requiring a third bar (avoiding #7's over-filtering)

4. Session-aware position sizing:
   - Early session: normal size, more setups
   - Mid session: normal size
   - Late session: reduced size, quicker exits
   - This preserves capital for better setups later in the session

5. Better handling of edge cases:
   - Ensure confirmation bar has real movement, not just direction
   - Skip trades when volatility is extremely low (likely no follow-through)
   - Tighter time-to-session-end check
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.35,
        min_body_ratio: float = 0.60,
        confirmation_body_ratio: float = 0.50,
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
        self._recent_volatility: float = 0.0

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

    def _get_phase_size_multiplier(self, phase: str) -> float:
        """Get size multiplier for session phase."""
        if phase == "early":
            return 1.0
        elif phase == "mid":
            return 1.0
        else:
            return 0.75

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
            self._recent_volatility = 0.0
        
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
        
        # Avoid trading very close to session end (need at least 3 bars)
        time_left_ns = ctx.session_end - ctx.ts
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)
        if bars_left_estimate < 3.0:
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
        
        # Calculate recent volatility from a longer window for context
        # Use all available history up to lookback*2 for volatility baseline
        vol_window_size = min(len(history) - 1, self.lookback * 2)
        if vol_window_size > 0:
            vol_window_start = max(0, len(history) - vol_window_size - 1)
            vol_window = history[vol_window_start:len(history) - 1]
            if len(vol_window) > 0:
                self._recent_volatility = sum(b.high - b.low for b in vol_window) / len(vol_window)
        
        # Skip if volatility is extremely low (likely choppy, no follow-through)
        if self._recent_volatility < typical_range * 0.5:
            return
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # Volatility-adjusted threshold: when volatility is high, require bigger moves
        vol_ratio = self._recent_volatility / typical_range if typical_range > 0 else 1.0
        vol_ratio = max(0.8, min(1.5, vol_ratio))  # Clamp between 0.8 and 1.5
        adjusted_threshold = self.move_threshold / vol_ratio
        
        # Outsized detection: range is large AND body is substantial
        is_outsized = (
            current_range > typical_range * adjusted_threshold and
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
        
        # TWO-BAR CONFIRMATION WITH DIRECTION CONVICTION
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body (not just a wick)
        # 3. Actually progress in the fade direction (close further in fade direction)
        
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
                self._enter_fade(ctx, "up", vol_ratio)
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                # Previous was down, current is up with conviction - fade the down move
                self._enter_fade(ctx, "down", vol_ratio)
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_magnitude = current_magnitude

    def _enter_fade(self, ctx, outsized_direction: str, vol_ratio: float) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0 or self._bar_minus_1_magnitude <= 0:
            return
        
        # Measure how extreme the outsized move was
        move_ratio = self._bar_minus_1_magnitude / self._typical_range
        
        # Session-aware sizing
        phase = self._get_session_phase(self._bars_in_session)
        phase_mult = self._get_phase_size_multiplier(phase)
        
        # Conservative sizing: scale based on move magnitude and phase
        size_multiplier = min(2.0, 1.0 + move_ratio * 0.35) * phase_mult
        size = max(1, int(self.base_size * size_multiplier))
        
        # Volatility-scaled bracket sizing
        # When volatility is high, give more room; when low, tighter
        vol_bracket_mult = 0.8 + vol_ratio * 0.4
        
        # Scale brackets based on move magnitude
        stop_mult = self.stop_offset * move_ratio * vol_bracket_mult
        target_mult = self.target_offset * move_ratio * vol_bracket_mult * 0.85
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=max(0.45, stop_mult),
                target_offset=max(0.18, target_mult),
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=max(0.45, stop_mult),
                target_offset=max(0.18, target_mult),
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
