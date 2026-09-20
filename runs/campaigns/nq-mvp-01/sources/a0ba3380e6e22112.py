"""
Mean-reversion fade with dual-confirmation and adaptive entry filtering.

Analysis of recent trials:
- #29: -0.1707 — best recent, strict body ratio (0.75) on outsized moves
- #30: -0.2675 — adaptive body ratio hurt performance
- #31: NOT SCORED — three-bar confirmation was too restrictive
- #6: -0.3638 — all-time champion with simpler logic

Key insight: The two-bar pattern in #29 works well, but we're being too selective
on the OUTSIZED move itself. The problem: we filter out moves that would revert,
just because they don't have a perfect body ratio.

New approach: RELAX the outsized move detection (lower body_ratio to 0.68),
but ADD a second confirmation layer on the REVERSAL bar itself:
1. Bar N: Outsized move (relaxed threshold, body_ratio 0.68)
2. Bar N+1: Reversal begins AND shows strong conviction
   - Must be opposite direction
   - Must have EXCELLENT body (0.70+)
   - Must close beyond the prior open (progression)
   - Must show STRONG reversal strength (0.25+)

This way we catch more outsized moves but only trade the ones followed by
HIGH-CONVICTION reversals. The reversal bar becomes the real filter.

Additional refinements:
- Move threshold: 1.38 (middle ground between 1.35 and 1.40)
- Base body ratio: 0.68 (relaxed from 0.75)
- Confirmation body ratio: 0.70 (STRICT on the reversal bar)
- Reversal strength threshold: 0.25 (STRICT, only high-conviction reversals)
- This filters on conviction of the reversal, not weakness of the move
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.38,
        min_body_ratio: float = 0.68,
        confirmation_body_ratio: float = 0.70,
        base_size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        reversal_strength_threshold: float = 0.25,
        min_typical_range: float = 0.10,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.reversal_strength_threshold = reversal_strength_threshold
        self.min_typical_range = min_typical_range
        
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
        
        # Avoid trading in first 4 bars (need session context)
        if self._bars_in_session < 5:
            return
        
        # Avoid trading in last 4 bars (need time for mean reversion)
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
        if typical_range < self.min_typical_range:
            return
        
        self._typical_range = typical_range
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # RELAXED outsized detection: catch more moves with lower body ratio
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
        
        # TWO-BAR CONFIRMATION WITH STRICT REVERSAL CONVICTION FILTER
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have EXCELLENT body (0.70+)
        # 3. Close beyond prior bar's open (progression)
        # 4. Show STRONG reversal conviction (threshold 0.25)
        
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # Check body quality of confirmation bar - STRICT on reversal
            has_excellent_body = (
                current_body > current_range * self.confirmation_body_ratio
                if current_range > 0 else False
            )
            
            # Check for reversal confirmation with progression and STRONG conviction
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_excellent_body):
                # Previous was up, current is down: check if close is below prior open
                if current_bar.close < self._bar_minus_1_open:
                    # Measure reversal strength - STRICT threshold
                    reversal_range = self._bar_minus_1_open - current_bar.low if current_bar.low < self._bar_minus_1_open else 0.1
                    reversal_distance = self._bar_minus_1_open - current_bar.close
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        ctx.sell(
                            size=self.base_size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="fade-up",
                        )
            
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_excellent_body):
                # Previous was down, current is up: check if close is above prior open
                if current_bar.close > self._bar_minus_1_open:
                    # Measure reversal strength - STRICT threshold
                    reversal_range = current_bar.high - self._bar_minus_1_open if current_bar.high > self._bar_minus_1_open else 0.1
                    reversal_distance = current_bar.close - self._bar_minus_1_open
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
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
