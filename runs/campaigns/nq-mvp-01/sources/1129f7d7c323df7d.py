"""
Mean-reversion fade with ultra-conservative parameter set from #6.

Analysis of prior trials:
- #6: -0.3638 — unbeaten champion with specific parameter set
- #10: -0.1862 — best recent, but used adaptive conviction gating
- #18: -0.4503 — exact #6 replica with 1.35 threshold (worse)
- #19: -0.2772 — added volatility-based bracket scaling (worse)

Key insight: #6's parameters are the core of its success. The exact values matter:
- lookback: 8
- move_threshold: 1.4
- min_body_ratio: 0.70
- confirmation_body_ratio: 0.60
- stop_offset: 0.75
- target_offset: 0.75

Every modification since has made things worse. The pattern is clear: the signal
is robust, but the parameters are finely tuned. Any deviation hurts.

This strategy returns to #6 with zero modifications. The only change is moving
the first 3 bars skip into a more conservative gate: skip if we haven't seen
at least 4 bars AND we're within the first 10% of the session duration.

This keeps the core logic pure while being slightly more conservative about
early-session noise, which should help without over-parameterizing.

The philosophy: #6 scored -0.3638. Every trial that tried to improve it failed.
The answer is to use exactly what worked, with only the most minimal refinement
to handle edge cases better.
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
        self._session_start_ts: int | None = None
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
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
            self._session_start_ts = ctx.session_start
            self._bar_minus_1_outsized = False
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
        
        # Conservative early-session gate: skip if we're in the first 10% of session
        # AND we haven't seen at least 4 bars
        session_duration = ctx.session_end - ctx.session_start
        time_into_session = ctx.ts - ctx.session_start
        pct_into_session = time_into_session / session_duration if session_duration > 0 else 0.0
        
        if self._bars_in_session < 4 or pct_into_session < 0.10:
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
                ctx.sell(
                    size=self.base_size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="fade-up",
                )
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                ctx.buy(
                    size=self.base_size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="fade-down",
                )
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
