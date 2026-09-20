"""
Mean-reversion fade with adaptive market regime filtering.

Analysis of prior trials:
- #6: -0.3638 — best so far, two-bar confirmation with body-quality filtering
- #10: -0.1862 — recent attempt with tighter confirmation and progression

Key insight from #6 vs #10: #6 achieved -0.3638 with simpler logic. #10's score 
of -0.1862 suggests the stricter progression requirement (close beyond prior open)
and higher conviction threshold (0.3) are filtering out too many valid trades or
creating false negatives.

The core mean-reversion signal is sound, but we're being too selective. This 
strategy refines the approach by:

1. Relaxed confirmation requirements:
   - Revert to simpler reversal detection (just opposite direction + good body)
   - Don't require close beyond prior open (that's too strict)
   - This recovers trades that #10 was filtering out

2. Lower conviction threshold:
   - Accept reversals with conviction >= 0.2 (was 0.3 in #10)
   - This trades more frequently but on still-meaningful reversals
   - More trades = better statistical power

3. Adaptive market regime detection:
   - Measure session volatility: is this a high-vol or low-vol session?
   - In high-vol sessions: use larger move_threshold (1.5) to avoid noise
   - In low-vol sessions: use lower move_threshold (1.2) to catch all moves
   - This keeps signal-to-noise ratio consistent across market conditions

4. Simplified bracket logic:
   - Return to more generous bracket sizing (like #6)
   - Size scaling: conviction-based without excessive magnitude scaling
   - Targets: 0.40 (like #6) rather than 0.35

5. Smarter bar counting:
   - Don't skip last 3 bars if they have sufficient time
   - Use actual time remaining rather than estimate
   - This captures late-session mean reversions

6. Session context:
   - Track actual range/volatility in current session
   - Use that for dynamic thresholds rather than fixed parameters
   - Adapt to how THIS session is behaving
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        base_move_threshold: float = 1.35,
        min_body_ratio: float = 0.65,
        confirmation_body_ratio: float = 0.55,
        base_size: int = 1,
        stop_offset: float = 0.80,
        target_offset: float = 0.40,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.base_move_threshold = base_move_threshold
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._session_bars: list = []
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_magnitude: float = 0.0
        self._bar_minus_1_open: float = 0.0
        self._typical_range: float = 0.0
        self._session_volatility: float = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Reset on new session
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_in_session = 0
            self._session_bars = []
            self._bar_minus_1_outsized = False
            self._bar_minus_1_direction = None
            self._bar_minus_1_magnitude = 0.0
            self._bar_minus_1_open = 0.0
            self._typical_range = 0.0
            self._session_volatility = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        self._session_bars.append(current_bar)
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Avoid trading in first 2 bars (need session context)
        if self._bars_in_session < 3:
            return
        
        # Check time remaining - be less strict than #10
        time_left_ns = ctx.session_end - ctx.ts
        min_time_needed_ns = 10 * 60 * 1e9  # 10 minutes minimum
        if time_left_ns < min_time_needed_ns:
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
        
        # Calculate session volatility (rolling average of ranges in session so far)
        if len(self._session_bars) >= 3:
            recent_session_ranges = [b.high - b.low for b in self._session_bars[-5:]]
            self._session_volatility = sum(recent_session_ranges) / len(recent_session_ranges)
        else:
            self._session_volatility = typical_range
        
        # Adaptive move_threshold based on session volatility
        # High volatility: require larger moves (1.5x) to avoid noise
        # Low volatility: accept smaller moves (1.2x) to catch all reversals
        vol_ratio = self._session_volatility / typical_range if typical_range > 0 else 1.0
        if vol_ratio > 1.2:  # High volatility session
            move_threshold = 1.5
        elif vol_ratio < 0.8:  # Low volatility session
            move_threshold = 1.2
        else:  # Normal volatility
            move_threshold = self.base_move_threshold
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # Outsized detection
        is_outsized = (
            current_range > typical_range * move_threshold and
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
        
        # TWO-BAR CONFIRMATION (simplified from #10)
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body
        # (Removed: requirement to close beyond prior open - too strict)
        
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
                self._enter_fade(ctx, "up", current_bar)
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
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
        
        # Conviction score: 0.0 to 1.0
        conviction = min(1.0, max(0.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5))
        
        # Lower conviction threshold (0.2 instead of 0.3) to trade more
        if conviction < 0.2:
            return
        
        # Size based on conviction (simpler than #10)
        size_multiplier = 1.0 + conviction * 0.5
        size = max(1, int(self.base_size * min(2.0, size_multiplier)))
        
        # Bracket sizing: simpler, more generous like #6
        stop_mult = self.stop_offset * move_ratio
        target_mult = self.target_offset * move_ratio
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=max(0.50, stop_mult),
                target_offset=max(0.20, target_mult),
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=max(0.50, stop_mult),
                target_offset=max(0.20, target_mult),
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
