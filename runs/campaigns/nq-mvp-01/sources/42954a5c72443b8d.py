"""
Mean-reversion fade with refined two-bar confirmation and parameter tuning.

Analysis of prior trials:
- #6: -0.3638 — champion with two-bar confirmation
- #10: -0.1862 — best recent, but conviction gating was too restrictive
- #23: -0.2469 — adaptive sizing diluted the signal

Key insight: #6's simplicity is unbeatable. The two-bar confirmation pattern
(outsized move followed by reversal with progression) is the core strength.

However, examining the parameter space:
- #6 used move_threshold=1.4, which is quite strict
- #23 tried 1.25, which was too loose and scored worse
- The sweet spot may be around 1.3

This strategy refines #6 by:

1. Fine-tune move detection threshold:
   - Use move_threshold=1.3 (between #6's 1.4 and #23's 1.25)
   - This catches more outsized moves without being too loose
   - Stricter body ratio (0.72) to maintain quality

2. Tighter confirmation requirements:
   - Keep confirmation_body_ratio at 0.60 (proven in #6)
   - Require current close to be beyond prior open (progression requirement)
   - This is the core pattern that works

3. Conservative entry timing:
   - Skip first 4 bars (session context)
   - Skip last 4 bars (time for mean reversion)
   - This is consistent with what works

4. Fixed, simple sizing:
   - Return to base_size=1 with no adaptive scaling
   - Let the bracket handle the risk/reward
   - Simplicity is the strength

5. Balanced bracket parameters:
   - stop_offset=0.75, target_offset=0.75
   - These are proven parameters from #6
   - No complex scaling, just direct application

The philosophy: #6 scored -0.3638 because it's simple and captures real
reversals. Every attempt to improve through complexity has failed. This
proposal makes only the minimal parameter adjustment (threshold 1.4→1.3)
that might catch a few more high-quality trades without over-fitting.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.3,
        min_body_ratio: float = 0.72,
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
        
        # Skip last 4 bars (need time for mean reversion)
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
        if typical_range <= 0.1:
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
        current_open = current_bar.open
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        
        # TWO-BAR CONFIRMATION
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body
        # 3. Close beyond prior bar's open (progression)
        
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
                    ctx.sell(
                        size=self.base_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="fade-up",
                    )
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                # Previous was down, current is up: check if close is above prior open
                if current_bar.close > self._bar_minus_1_open:
                    ctx.buy(
                        size=self.base_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="fade-down",
                    )
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_open = current_open

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
