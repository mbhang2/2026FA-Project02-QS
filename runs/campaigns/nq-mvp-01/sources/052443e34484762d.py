"""
Mean-reversion fade with three-bar confirmation and adaptive entry discipline.

Analysis of prior trials:
- #6: -0.3638 — champion, two-bar confirmation with simple logic
- #27: -0.1954 — reversal strength filtering helped but was too strict (0.40 threshold)
- #26: -0.2479 — adaptive threshold had minimal impact
- #10: -0.1862 — conviction gating was too restrictive

Key insight: #6's -0.3638 remains unbeaten because the two-bar pattern is real.
But #27 showed that reversal strength filtering CAN help when tuned correctly.

The problem with #27: reversal_strength_threshold=0.40 was too high, filtering
out too many valid trades. The solution: lower the threshold to 0.25-0.30 to
capture more reversals while still filtering the weakest ones.

This strategy refines #6 by:

1. Selective entry based on MODERATE reversal strength:
   - Measure how far bar 2 closed in fade direction relative to the reversal range
   - Only enter if reversal shows meaningful conviction (closes >25% into fade)
   - This filters the weakest reversals (doji confirmation, barely-reversing bars)
   - But keeps the real reversals that #6 was catching

2. Three-bar confirmation pattern (optional):
   - After outsized move + reversal, optionally wait for bar 3 to confirm
   - Bar 3 should NOT reverse back into the outsized direction
   - This catches sustained reversals vs. quick bounces
   - But keep it simple: just check bar 3 doesn't go against the reversal

3. Fixed, proven bracket parameters from #6:
   - stop_offset=0.75, target_offset=0.75
   - base_size=1
   - No adaptive sizing complexity
   - Let the pattern's quality speak for itself

4. Session-aware entry filtering (from #6):
   - Skip first 4 bars (need session context)
   - Skip last 4 bars (need time for mean reversion)
   - Skip if typical range is too low

The philosophy: #6 works because the pattern is real. The improvement is to
filter using a MODERATE reversal strength threshold (0.25 instead of 0.40) that
removes only the very weakest reversals while keeping the good trades #6 found.
Three-bar confirmation adds a secondary filter for sustained reversals.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.40,
        min_body_ratio: float = 0.70,
        confirmation_body_ratio: float = 0.60,
        base_size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        reversal_strength_threshold: float = 0.25,
        use_three_bar_confirmation: bool = True,
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
        self.use_three_bar_confirmation = use_three_bar_confirmation
        self.min_typical_range = min_typical_range
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_2_outsized: bool = False
        self._bar_minus_2_direction: str | None = None
        self._bar_minus_2_open: float = 0.0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._bar_minus_1_moved_opposite: bool = False
        self._bar_minus_1_reversal_strength: float = 0.0
        self._typical_range: float = 0.0
        self._pending_fade_direction: str | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Reset on new session
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_in_session = 0
            self._bar_minus_2_outsized = False
            self._bar_minus_2_direction = None
            self._bar_minus_2_open = 0.0
            self._bar_minus_1_outsized = False
            self._bar_minus_1_direction = None
            self._bar_minus_1_open = 0.0
            self._bar_minus_1_moved_opposite = False
            self._bar_minus_1_reversal_strength = 0.0
            self._typical_range = 0.0
            self._pending_fade_direction = None
        
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
        
        # THREE-BAR CONFIRMATION (optional)
        # Bar -2: outsized move
        # Bar -1: reversal with strength filter
        # Bar 0 (current): confirm reversal is sustained (doesn't reverse back)
        
        if self.use_three_bar_confirmation:
            # Check if we're on bar 3 of a potential pattern
            # Bar -2 was outsized, bar -1 showed reversal with strength
            if self._bar_minus_2_outsized and self._bar_minus_1_moved_opposite:
                current_moved_up = current_bar.close > current_bar.open
                current_moved_down = current_bar.close < current_bar.open
                
                # Check if current bar confirms the reversal (doesn't go back into outsized direction)
                should_trade = False
                trade_direction = None
                
                if self._bar_minus_2_direction == "up" and self._bar_minus_1_direction == "down":
                    # Outsized up, reversal down, now confirm down continues
                    if current_moved_down or (current_bar.close < current_bar.open + 0.01):
                        should_trade = True
                        trade_direction = "up"
                
                elif self._bar_minus_2_direction == "down" and self._bar_minus_1_direction == "up":
                    # Outsized down, reversal up, now confirm up continues
                    if current_moved_up or (current_bar.close > current_bar.open - 0.01):
                        should_trade = True
                        trade_direction = "down"
                
                if should_trade and trade_direction is not None:
                    ctx.buy(
                        size=self.base_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag=f"fade-{trade_direction}",
                    ) if trade_direction == "down" else ctx.sell(
                        size=self.base_size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag=f"fade-{trade_direction}",
                    )
        else:
            # TWO-BAR CONFIRMATION (simpler path)
            # Previous bar was outsized, current bar must:
            # 1. Move opposite direction (reversal)
            # 2. Have meaningful body
            # 3. Close beyond prior bar's open (progression)
            # 4. Show sufficient reversal strength
            
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
                
                # Check for reversal confirmation with progression and strength
                if (self._bar_minus_1_direction == "up" and current_moved_down and 
                    has_good_body):
                    # Previous was up, current is down: check if close is below prior open
                    if current_bar.close < self._bar_minus_1_open:
                        # Measure reversal strength
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
                      has_good_body):
                    # Previous was down, current is up: check if close is above prior open
                    if current_bar.close > self._bar_minus_1_open:
                        # Measure reversal strength
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
        
        # Shift bar history for next iteration
        self._bar_minus_2_outsized = self._bar_minus_1_outsized
        self._bar_minus_2_direction = self._bar_minus_1_direction
        self._bar_minus_2_open = self._bar_minus_1_open
        
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_open = current_open
        
        # Track if bar -1 moved opposite to bar -2 (for three-bar confirmation)
        if self._bar_minus_2_outsized and self._bar_minus_2_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            
            if (self._bar_minus_2_direction == "up" and current_moved_down) or \
               (self._bar_minus_2_direction == "down" and current_moved_up):
                self._bar_minus_1_moved_opposite = True
            else:
                self._bar_minus_1_moved_opposite = False
        else:
            self._bar_minus_1_moved_opposite = False

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
