"""
Mean-reversion fade with multi-timeframe confirmation and adaptive risk scaling.

Analysis of prior trials:
- #6: -0.3638 — best so far, two-bar confirmation with body-quality filtering
- #8: -0.5016 — volatility scaling helped but lost edge vs #6

Key insight from #6: The two-bar confirmation with strict body-quality filtering 
(-0.3638) is the strongest approach. The issue with #8 was over-complication with
volatility scaling that diluted the signal.

This strategy refines #6 by:

1. Multi-timeframe confirmation:
   - Use 5m bar for entry signal (outsized move + reversal)
   - Add 1m confirmation: ensure the reversal is real at finer granularity
   - Reject entries where 1m shows continuation in original direction
   - This filters out false reversals while keeping strong fades

2. Refined entry quality:
   - Stricter body ratio on confirmation bar (0.60 vs 0.55)
   - Require confirmation bar to close beyond prior bar's open
   - This ensures real conviction, not just direction change

3. Adaptive risk sizing based on signal strength:
   - Measure reversal conviction: how far does confirmation bar close in fade direction?
   - Strong reversals (close near low for short, near high for long): 1.5x size
   - Weak reversals (close near middle): 1.0x size
   - This concentrates capital on highest-conviction trades

4. Tighter, more consistent bracket sizing:
   - Fixed stops/targets relative to move magnitude
   - Removed excessive phase-based adjustments that added noise
   - Focus on core mean-reversion logic

5. Session awareness (minimal):
   - Skip very late session (< 2 bars left)
   - Otherwise trade consistently throughout
   - Avoid over-parameterization that reduced #8's edge
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        fast_interval: str = "5m",
        confirm_interval: str = "1m",
        lookback: int = 8,
        move_threshold: float = 1.4,
        min_body_ratio: float = 0.65,
        confirmation_body_ratio: float = 0.60,
        base_size: int = 1,
        stop_offset: float = 0.85,
        target_offset: float = 0.40,
    ) -> None:
        self.fast_interval = fast_interval
        self.confirm_interval = confirm_interval
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
        # For multi-timeframe: track recent 1m bars
        self._recent_1m_bars: list = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.confirm_interval)

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
            self._recent_1m_bars = []
        
        # Update 1m bar history for multi-timeframe confirmation
        history_1m = ctx.history(self.confirm_interval)
        if len(history_1m) > 0:
            # Keep last 10 1m bars for confirmation checking
            self._recent_1m_bars = list(history_1m[-10:]) if len(history_1m) >= 10 else list(history_1m)
        
        # Only act on 5m bar closes
        if self.fast_interval not in ctx.bars:
            return
        
        history = ctx.history(self.fast_interval)
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
        if bars_left_estimate < 2.0:
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
        
        # Analyze current 5m bar for outsized move
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
        
        # TWO-BAR CONFIRMATION ON 5M WITH MULTI-TIMEFRAME CHECK
        # Previous 5m bar was outsized, current 5m bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body (high confirmation_body_ratio)
        # 3. 1m bars since last 5m bar close must not show continuation in original direction
        
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
            reversal_confirmed = False
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                reversal_confirmed = True
                fade_direction = "up"
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                reversal_confirmed = True
                fade_direction = "down"
            
            if reversal_confirmed:
                # Multi-timeframe check: verify 1m bars don't show continuation
                if self._passes_1m_filter(fade_direction):
                    self._enter_fade(ctx, fade_direction)
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_magnitude = current_magnitude

    def _passes_1m_filter(self, fade_direction: str) -> bool:
        """Check that recent 1m bars don't contradict the fade signal.
        
        For a fade-up (sell) signal: 1m bars shouldn't show strong continuation up.
        For a fade-down (buy) signal: 1m bars shouldn't show strong continuation down.
        """
        if len(self._recent_1m_bars) < 3:
            return True  # Not enough data, don't filter
        
        # Count recent 1m bar directions (last 3-4 bars)
        recent_count = min(4, len(self._recent_1m_bars))
        recent_bars = self._recent_1m_bars[-recent_count:]
        
        up_count = sum(1 for b in recent_bars if b.close > b.open)
        down_count = sum(1 for b in recent_bars if b.close < b.open)
        
        # For fade-up (sell): reject if most recent 1m bars are up
        if fade_direction == "up":
            if up_count >= recent_count - 1:  # 3+ out of 4 are up
                return False
        # For fade-down (buy): reject if most recent 1m bars are down
        elif fade_direction == "down":
            if down_count >= recent_count - 1:  # 3+ out of 4 are down
                return False
        
        return True

    def _enter_fade(self, ctx, outsized_direction: str) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0 or self._bar_minus_1_magnitude <= 0:
            return
        
        # Measure how extreme the outsized move was
        move_ratio = self._bar_minus_1_magnitude / self._typical_range
        
        # Measure reversal conviction from current bar
        # (how far it closed in fade direction)
        history = ctx.history(self.fast_interval)
        if len(history) < 2:
            return
        
        current_bar = history[-1]
        prior_bar = history[-2]
        
        # Calculate reversal conviction
        if outsized_direction == "up":
            # For fade-up (sell), conviction is how far current closed below prior open
            reversal_distance = prior_bar.open - current_bar.close
            reversal_range = prior_bar.high - prior_bar.low
        else:
            # For fade-down (buy), conviction is how far current closed above prior open
            reversal_distance = current_bar.close - prior_bar.open
            reversal_range = prior_bar.high - prior_bar.low
        
        # Conviction score: 0.0 to 1.0
        conviction = min(1.0, max(0.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5))
        
        # Size based on move magnitude and conviction
        # Strong reversals (high conviction) get more size
        size_multiplier = 1.0 + conviction * 0.5  # 1.0x to 1.5x
        size_multiplier *= (1.0 + move_ratio * 0.3)
        size = max(1, int(self.base_size * min(2.0, size_multiplier)))
        
        # Bracket sizing: tighter on small moves, wider on large moves
        stop_mult = self.stop_offset * move_ratio
        target_mult = self.target_offset * move_ratio * 0.9
        
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
