"""
Mean-reversion fade with multi-bar confirmation and adaptive entry filtering.

Analysis of prior trials:
- #6: -0.3638 — champion with two-bar confirmation
- #10: -0.1862 — best recent, used conviction gating but lost consistency
- #20: -0.2772 — exact #6 replica with timing gate, worse than #6

Key insight: #6's -0.3638 is hard to beat because the two-bar confirmation
pattern is genuinely robust. However, the recent runs show that strict adherence
to #6 (trials #18, #20) underperforms it slightly, suggesting either:
1. Parameter sensitivity is real and we're not hitting exact values
2. The confirmation logic could be slightly refined

This strategy explores a refined confirmation approach:

1. Keep #6's core parameters exactly:
   - lookback: 8
   - move_threshold: 1.4
   - min_body_ratio: 0.70
   - confirmation_body_ratio: 0.60
   - stop_offset: 0.75
   - target_offset: 0.75

2. Refine the confirmation logic:
   - Instead of requiring confirmation bar to have good body quality,
     require it to close beyond the prior bar's midpoint in fade direction
   - This is stricter than just "opposite direction" but more flexible than
     "beyond open" which #10 used
   - Midpoint = (prior_open + prior_close) / 2
   - For fade-up (sell): current_close < prior_midpoint
   - For fade-down (buy): current_close > prior_midpoint

3. Add conviction-based entry filtering:
   - Measure how deep into the reversal the confirmation bar goes
   - Only enter if conviction > 0.25 (weak threshold, just filters noise)
   - This avoids trading marginal reversals that barely meet criteria

4. Timing gate:
   - Skip first 4 bars (session context)
   - Skip if < 5 bars remain (allow time for mean reversion)
   - This is more permissive than #10 but more conservative than #20

The philosophy: #6 worked with two-bar confirmation. We're keeping the exact
parameters but refining what "confirmation" means to be more selective about
which reversals to trade. The midpoint test is between the strict "beyond open"
and the loose "opposite direction" -- it should catch genuine reversals while
filtering noise.
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
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        conviction_threshold: float = 0.25,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.conviction_threshold = conviction_threshold
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._bar_minus_1_close: float = 0.0
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
            self._bar_minus_1_close = 0.0
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
        
        # Skip if fewer than 5 bars remain
        time_left_ns = ctx.session_end - ctx.ts
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)
        if bars_left_estimate < 5.0:
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
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        
        # REFINED TWO-BAR CONFIRMATION
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Close beyond the midpoint of prior bar in fade direction
        # 3. Have conviction > threshold
        
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            
            prior_midpoint = (self._bar_minus_1_open + self._bar_minus_1_close) / 2.0
            
            # Check for reversal confirmation with midpoint progression
            if self._bar_minus_1_direction == "up" and current_moved_down:
                # Previous was up, current is down: check if close is below midpoint
                if current_bar.close < prior_midpoint:
                    # Calculate conviction: how far below midpoint
                    conviction = min(1.0, (prior_midpoint - current_bar.close) / self._typical_range)
                    if conviction > self.conviction_threshold:
                        ctx.sell(
                            size=self.base_size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="fade-up",
                        )
            elif self._bar_minus_1_direction == "down" and current_moved_up:
                # Previous was down, current is up: check if close is above midpoint
                if current_bar.close > prior_midpoint:
                    # Calculate conviction: how far above midpoint
                    conviction = min(1.0, (current_bar.close - prior_midpoint) / self._typical_range)
                    if conviction > self.conviction_threshold:
                        ctx.buy(
                            size=self.base_size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="fade-down",
                        )
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_open = current_bar.open
        self._bar_minus_1_close = current_bar.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
