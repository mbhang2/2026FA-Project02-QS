"""
Mean-reversion fade with adaptive threshold and momentum-based sizing.

Analysis of prior trials:
- #6: -0.3638 — champion, two-bar confirmation with simple logic
- #10: -0.1862 — conviction gating was too restrictive
- #24: -0.2238 — threshold fine-tuning didn't help
- #25: -0.2469 (NOT SCORED) — three-bar pattern too restrictive

Key insight: #6's -0.3638 remains unbeaten. The two-bar confirmation pattern
(outsized move followed by reversal with progression) captures real mean reversion.

The problem with attempts to improve #6:
- Adding conviction gates or momentum requirements filters out too many trades
- Three-bar confirmation is too selective
- Adaptive sizing based on weak signals dilutes the edge
- Threshold tuning (1.3 vs 1.4) has minimal impact

This strategy returns to #6's core strength but makes ONE targeted improvement:

1. Adaptive move_threshold based on session volatility:
   - Sessions with high typical range should use higher threshold (1.5)
   - Sessions with low typical range should use lower threshold (1.2)
   - This matches the threshold to the market's own volatility
   - Avoids being too strict on quiet days, too loose on volatile days

2. Keep everything else from #6:
   - Two-bar confirmation pattern (outsized + reversal with progression)
   - Simple body ratio filters (0.70 and 0.60)
   - Fixed sizing (base_size=1)
   - Fixed bracket parameters (stop_offset=0.75, target_offset=0.75)
   - Conservative entry timing (skip first 4, last 4 bars)

3. Session-aware volatility scaling:
   - Calculate typical range early in session (first 8 bars)
   - Use it to adjust move_threshold dynamically
   - Captures the intuition that "outsized" is relative to the day's activity

The philosophy: #6 works because it's simple and captures real reversals.
The one improvement that makes sense is adapting the threshold to match
each session's volatility profile, without adding complexity to the
confirmation logic or sizing decisions.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        min_body_ratio: float = 0.70,
        confirmation_body_ratio: float = 0.60,
        base_size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        min_threshold: float = 1.2,
        max_threshold: float = 1.5,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._typical_range: float = 0.0
        self._session_typical_range: float = 0.0
        self._move_threshold: float = 1.4
        self._session_ranges: list[float] = []

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
            self._session_typical_range = 0.0
            self._move_threshold = 1.4
            self._session_ranges = []
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        
        # Track ranges for session volatility calculation
        current_range = current_bar.high - current_bar.low
        self._session_ranges.append(current_range)
        
        # Update session typical range every bar (used for adaptive threshold)
        if len(self._session_ranges) > 0:
            self._session_typical_range = sum(self._session_ranges) / len(self._session_ranges)
        
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
        if typical_range <= 0.1:
            return
        
        self._typical_range = typical_range
        
        # Adaptive threshold based on session volatility
        # High volatility sessions use higher threshold (fewer trades, higher quality)
        # Low volatility sessions use lower threshold (more opportunities)
        if self._session_typical_range > 0:
            # Normalize session volatility relative to a baseline
            # If session is 2x as volatile as baseline, use higher threshold
            volatility_ratio = self._session_typical_range / (typical_range if typical_range > 0 else 1.0)
            # Clamp between min and max threshold
            self._move_threshold = self.min_threshold + (self.max_threshold - self.min_threshold) * min(1.0, volatility_ratio / 2.0)
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # Outsized detection using adaptive threshold
        is_outsized = (
            current_range > typical_range * self._move_threshold and
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
