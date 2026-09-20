"""
Mean-reversion fade with confirmation refinement and adaptive stop/target.

Analysis of recent trials:
- #40: -0.0438 — slight regression from #39
- #39: -0.0304 — best so far with simple two-bar reversal pattern
- #38: -0.0653 — over-engineering hurt performance

Key insight: #39's simplicity is optimal. The regression in #40 suggests that
loosening the confirmation body ratio (0.56 vs 0.58) and lowering the move
threshold (1.30 vs 1.32) captured too many low-quality signals.

Hypothesis for #41: Return closer to #39's parameters but with one refinement:
The stop and target offsets (0.70/0.72) are fixed. What if we make them
adaptive based on the actual reversal strength measured? Stronger reversals
might warrant tighter stops and targets.

Strategy:
1. Revert move_threshold to 1.32 (from #39, was working)
2. Revert confirmation_body_ratio to 0.58 (from #39, was working)
3. Revert early_session_confirmation_boost to 0.08 (from #39, was working)
4. ADAPTIVE STOP/TARGET: Scale stop_offset and target_offset by reversal_strength
   - Strong reversals (strength > 0.25): use tighter stops/targets
   - Weak reversals (strength 0.17-0.25): use standard stops/targets
   - This matches the quality of the signal to the risk management

This is a minimal change to #39 that respects what was working while adding
one dimension of adaptation based on measured signal strength.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.32,
        min_body_ratio: float = 0.75,
        base_confirmation_body_ratio: float = 0.58,
        base_size: int = 1,
        stop_offset: float = 0.70,
        target_offset: float = 0.72,
        reversal_strength_threshold: float = 0.17,
        min_typical_range: float = 0.10,
        momentum_lookback: int = 4,
        momentum_threshold: float = 0.15,
        early_session_bars: int = 4,
        late_session_bars: int = 4,
        early_session_confirmation_boost: float = 0.08,
        confirmation_window_extension: int = 3,
        strong_reversal_threshold: float = 0.25,
        strong_reversal_stop_mult: float = 0.85,
        strong_reversal_target_mult: float = 0.90,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.base_confirmation_body_ratio = base_confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.reversal_strength_threshold = reversal_strength_threshold
        self.min_typical_range = min_typical_range
        self.momentum_lookback = momentum_lookback
        self.momentum_threshold = momentum_threshold
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        self.early_session_confirmation_boost = early_session_confirmation_boost
        self.confirmation_window_extension = confirmation_window_extension
        self.strong_reversal_threshold = strong_reversal_threshold
        self.strong_reversal_stop_mult = strong_reversal_stop_mult
        self.strong_reversal_target_mult = strong_reversal_target_mult
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._bar_minus_1_high: float = 0.0
        self._bar_minus_1_low: float = 0.0
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
            self._bar_minus_1_high = 0.0
            self._bar_minus_1_low = 0.0
            self._typical_range = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Avoid trading in first N bars (need session context)
        if self._bars_in_session < self.early_session_bars + 1:
            return
        
        # Avoid trading in last N bars (need time for mean reversion)
        time_left_ns = ctx.session_end - ctx.ts
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)
        if bars_left_estimate < self.late_session_bars:
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
        
        # Simple outsized detection: range is large AND body is substantial
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
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # Dynamic confirmation body ratio based on session age
            confirmation_ratio = self.base_confirmation_body_ratio
            if self._bars_in_session <= self.early_session_bars + self.confirmation_window_extension:
                confirmation_ratio += self.early_session_confirmation_boost
            
            # Check body quality of confirmation bar
            has_good_body = (
                current_body > current_range * confirmation_ratio
                if current_range > 0 else False
            )
            
            # Calculate session momentum from the last momentum_lookback bars
            session_momentum = None
            if len(history) >= self.momentum_lookback + 2:
                momentum_window_start = max(0, len(history) - self.momentum_lookback - 2)
                momentum_window_end = len(history) - 2
                momentum_window = history[momentum_window_start:momentum_window_end]
                
                if len(momentum_window) > 0:
                    momentum_closes = [b.close for b in momentum_window]
                    if len(momentum_closes) >= 2:
                        momentum_change = momentum_closes[-1] - momentum_closes[0]
                        momentum_range = max(b.high for b in momentum_window) - min(b.low for b in momentum_window)
                        if momentum_range > 0:
                            session_momentum = momentum_change / momentum_range
            
            # Check for reversal confirmation
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                # Previous was up, current is down
                if current_bar.close < self._bar_minus_1_open:
                    # Measure reversal strength
                    reversal_range = self._bar_minus_1_high - self._bar_minus_1_open if self._bar_minus_1_high > self._bar_minus_1_open else 0.1
                    reversal_distance = self._bar_minus_1_open - current_bar.close
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        # Apply momentum filter: only skip if momentum is VERY strong up
                        trade_signal = True
                        if session_momentum is not None:
                            if session_momentum > self.momentum_threshold:
                                trade_signal = False
                        
                        if trade_signal:
                            # Adaptive stop/target based on reversal strength
                            stop_offset = self.stop_offset
                            target_offset = self.target_offset
                            if reversal_strength > self.strong_reversal_threshold:
                                stop_offset = self.stop_offset * self.strong_reversal_stop_mult
                                target_offset = self.target_offset * self.strong_reversal_target_mult
                            
                            ctx.sell(
                                size=self.base_size,
                                stop_offset=stop_offset,
                                target_offset=target_offset,
                                tag="fade-up",
                            )
            
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                # Previous was down, current is up
                if current_bar.close > self._bar_minus_1_open:
                    # Measure reversal strength
                    reversal_range = self._bar_minus_1_open - self._bar_minus_1_low if self._bar_minus_1_open > self._bar_minus_1_low else 0.1
                    reversal_distance = current_bar.close - self._bar_minus_1_open
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        # Apply momentum filter: only skip if momentum is VERY strong down
                        trade_signal = True
                        if session_momentum is not None:
                            if session_momentum < -self.momentum_threshold:
                                trade_signal = False
                        
                        if trade_signal:
                            # Adaptive stop/target based on reversal strength
                            stop_offset = self.stop_offset
                            target_offset = self.target_offset
                            if reversal_strength > self.strong_reversal_threshold:
                                stop_offset = self.stop_offset * self.strong_reversal_stop_mult
                                target_offset = self.target_offset * self.strong_reversal_target_mult
                            
                            ctx.buy(
                                size=self.base_size,
                                stop_offset=stop_offset,
                                target_offset=target_offset,
                                tag="fade-down",
                            )
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_open = current_open
        self._bar_minus_1_high = current_bar.high
        self._bar_minus_1_low = current_bar.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
