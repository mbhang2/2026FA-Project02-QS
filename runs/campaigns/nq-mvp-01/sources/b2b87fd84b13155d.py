"""
Mean-reversion fade with multi-bar confirmation and adaptive conviction gating.

Analysis of prior trials:
- #6: -0.3638 — baseline two-bar confirmation, very clean
- #15: -0.4574 — fixed brackets with stricter filters
- #16: -0.4574 — session position awareness with reduced late-session sizing

Key insight: #6 at -0.3638 remains the best performer. The two-bar confirmation
pattern is robust, but all attempts to add complexity (progression requirements,
adaptive scaling, session-aware sizing) have degraded performance.

However, looking at the pattern of results, there's a consistent theme: the 
simplest, most focused approaches work best. #6 succeeded because it had:
1. Clear, unambiguous entry signal (outsized move + reversal with body quality)
2. Fixed, consistent bracket sizing
3. Minimal parameterization

This strategy returns to #6's core logic but makes one targeted refinement:

1. Keep #6's proven two-bar confirmation exactly:
   - Outsized move detection: range > 1.35x typical, body > 65% of range
   - Reversal confirmation: opposite direction, body > 60% of range
   - No progression requirement, no conviction gates

2. Refine typical range calculation:
   - Use 10-bar lookback instead of 8 (slightly more context)
   - This gives a more stable baseline for move detection

3. Simplify entry conditions:
   - Skip first 3 bars (need context)
   - Skip if < 5 bars remain (need time for mean reversion)
   - No complex time-based gates

4. Keep bracket logic identical to #6:
   - Stop offset: 0.75
   - Target offset: 0.75
   - Size: 1 contract (minimal complexity)

5. Focus on signal purity:
   - Only trade high-conviction setups
   - Require typical_range > 0.10 (filter dead markets)
   - No adaptive scaling that might dilute the signal

The philosophy: #6 works because it's a pure, focused signal. Every modification
that tried to be "smarter" has made results worse. This proposal keeps the exact
same logic but with slightly better parameters derived from the consistent
underperformance of variations. The key is that simplicity and consistency beat
adaptive complexity.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 10,
        move_threshold: float = 1.35,
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
        
        # Need at least 5 bars remaining for mean reversion to play out
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
        current_magnitude = 0.0
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
                current_magnitude = current_bar.close - current_bar.open
            elif current_bar.close < current_bar.open:
                current_direction = "down"
                current_magnitude = current_bar.open - current_bar.close
        
        # TWO-BAR CONFIRMATION (exactly #6 logic)
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
                self._enter_fade(ctx, "up")
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                self._enter_fade(ctx, "down")
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_magnitude = current_magnitude

    def _enter_fade(self, ctx, outsized_direction: str) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0 or self._bar_minus_1_magnitude <= 0:
            return
        
        # Fixed size and brackets - this is what made #6 work
        size = self.base_size
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
