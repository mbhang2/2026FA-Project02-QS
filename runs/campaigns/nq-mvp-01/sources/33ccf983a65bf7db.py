"""
Mean-reversion fade with refined session position awareness and conviction scaling.

Analysis of prior trials:
- #6: -0.3638 — baseline two-bar confirmation, very clean
- #10: -0.1862 — progression requirement too strict, killed signal
- #14: -0.3108 — adaptive bracket scaling diluted results
- #15: -0.2189 — fixed brackets with stricter filters still underperformed #6

Key insight: #6's -0.3638 remains best. The core pattern works:
- Two-bar confirmation: outsized move + reversal with body quality
- Simple, focused signal without complexity

This strategy refines #6 with a targeted improvement:

1. Keep #6's proven logic exactly:
   - Two-bar pattern: outsized move + reversal with good body
   - Strict body filtering (65% for move, 60% for confirmation)
   - Fixed bracket offsets (0.75 stop, 0.75 target)

2. Add session position awareness:
   - Early session (bars 1-3): skip (need context)
   - Core session (bars 4-N-4): trade normally
   - Late session (last 4 bars): reduce position sizing
   - This captures the fade signal where it works best

3. Refine move magnitude conviction:
   - Only trade if outsized move is > 1.3x typical range (slightly stricter)
   - Size by magnitude: 1 + (move_ratio * 0.1) for subtle scaling
   - This keeps the signal tight without adaptive bracket complexity

4. Better typical range calculation:
   - Use last 8 bars (proven lookback from #6)
   - Require minimum 0.10 range (not too restrictive)
   - Exclude current bar from calculation

5. Simpler session-aware exit windows:
   - Skip first 3 bars (need context)
   - Skip last 4 bars (not enough time for reversion)
   - No time-based gates (simpler logic)

The philosophy: #6 works because it's simple and focused. This proposal
keeps that simplicity but adds session position awareness - trading the
core window where mean reversion has time to play out, reducing exposure
near close. No adaptive scaling, no complex gates, just better positioning
within the session.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.30,
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
        self._bar_minus_1_magnitude: float = 0.0
        self._typical_range: float = 0.0
        self._session_bar_count: int = 0

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
            self._session_bar_count = 0
        
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
        
        # Estimate bars remaining in session (5m bars)
        # Session is ~6.5 hours = 78 5-minute bars
        # Avoid trading in last 4 bars to give reversion time to play out
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
        current_magnitude = 0.0
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
                current_magnitude = current_bar.close - current_bar.open
            elif current_bar.close < current_bar.open:
                current_direction = "down"
                current_magnitude = current_bar.open - current_bar.close
        
        # TWO-BAR CONFIRMATION (#6 logic)
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
                self._enter_fade(ctx, "up", bars_left_estimate)
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                self._enter_fade(ctx, "down", bars_left_estimate)
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_magnitude = current_magnitude

    def _enter_fade(self, ctx, outsized_direction: str, bars_left: float) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0 or self._bar_minus_1_magnitude <= 0:
            return
        
        # Measure how extreme the outsized move was
        move_ratio = self._bar_minus_1_magnitude / self._typical_range
        
        # Size by move magnitude - subtle scaling
        # Larger moves get slightly larger positions
        size_multiplier = 1.0 + move_ratio * 0.1
        size = max(1, int(self.base_size * min(1.5, size_multiplier)))
        
        # Reduce size in late session (last 4-8 bars)
        # When bars_left is 4-8, we're in the tail - reduce exposure
        if bars_left < 8.0:
            size = max(1, int(size * 0.7))
        
        # Fixed brackets - this is what made #6 work
        stop_offset = self.stop_offset
        target_offset = self.target_offset
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
