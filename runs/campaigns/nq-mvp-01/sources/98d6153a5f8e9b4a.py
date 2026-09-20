"""
Mean-reversion fade with refined confirmation and tighter parameter discipline.

Analysis of prior trials:
- #6: -0.3638 — best so far, two-bar confirmation with body-quality filtering
- #8: -0.5016 — volatility scaling diluted the signal
- #9: -0.3644 — multi-timeframe added complexity without improvement

Key insight: #6's simplicity is the strength. The two-bar confirmation with 
strict body-quality filtering (-0.3638) works because it focuses on high-conviction 
reversals without over-parameterization.

This strategy refines #6 by:

1. Tighter confirmation bar requirements:
   - Require confirmation bar to close BEYOND prior bar's open (not just opposite direction)
   - This ensures the reversal is progressing, not just starting
   - Filter out doji/small-range confirmation bars more aggressively

2. Better sizing discipline:
   - Measure reversal conviction: how far confirmation bar closed in fade direction
   - Size scales with both move magnitude AND reversal conviction
   - Avoid trading weak reversals at all

3. More selective entry timing:
   - Skip trades in first 3 bars (need more session context)
   - Skip trades in last 3 bars (not enough time for mean reversion to play out)
   - This concentrates trades in the core session window where mean reversion works best

4. Simplified bracket logic:
   - Remove unnecessary multipliers and adjustments
   - Use fixed ratios scaled only by move magnitude
   - Tighter targets (0.35 vs 0.40) to lock in profits more consistently

5. Better handling of edge cases:
   - Require minimum typical range to avoid choppy markets
   - Stricter body ratio on outsized move detection (0.70 vs 0.65)
   - This filters out weak outsized moves that lead to poor fades
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
        stop_offset: float = 0.85,
        target_offset: float = 0.35,
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
        
        # Avoid trading in first 3 bars (need session context)
        if self._bars_in_session < 4:
            return
        
        # Avoid trading in last 3 bars (need time for mean reversion)
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
        if typical_range <= 0.1:  # Skip extremely choppy/low-range markets
            return
        
        self._typical_range = typical_range
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # Stricter outsized detection: range is large AND body is substantial
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
        
        # TWO-BAR CONFIRMATION WITH PROGRESSION REQUIREMENT
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body
        # 3. Close BEYOND prior bar's open (showing progression in fade direction)
        
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
        
        # Measure reversal conviction: how far current closed in fade direction
        if outsized_direction == "up":
            # For fade-up (sell), conviction is how far below prior open
            reversal_distance = self._bar_minus_1_open - current_bar.close
            reversal_range = self._bar_minus_1_open - current_bar.low if current_bar.low < self._bar_minus_1_open else 0.1
        else:
            # For fade-down (buy), conviction is how far above prior open
            reversal_distance = current_bar.close - self._bar_minus_1_open
            reversal_range = current_bar.high - self._bar_minus_1_open if current_bar.high > self._bar_minus_1_open else 0.1
        
        # Conviction score: 0.0 to 1.0 (how far into the reversal range)
        conviction = min(1.0, max(0.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5))
        
        # Size based on move magnitude and reversal conviction
        # Only trade if conviction is reasonable (above 0.3)
        if conviction < 0.3:
            return
        
        size_multiplier = 1.0 + conviction * 0.4 + move_ratio * 0.2
        size = max(1, int(self.base_size * min(2.0, size_multiplier)))
        
        # Bracket sizing: scale by move magnitude and conviction
        stop_mult = self.stop_offset * move_ratio * (0.8 + conviction * 0.4)
        target_mult = self.target_offset * move_ratio * (0.8 + conviction * 0.3)
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=max(0.50, stop_mult),
                target_offset=max(0.18, target_mult),
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=max(0.50, stop_mult),
                target_offset=max(0.18, target_mult),
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
