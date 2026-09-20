"""
Mean-reversion fade with adaptive reversal strength and session momentum filtering.

Analysis of recent trials:
- #29: -0.1707 — best recent, strict body ratio (0.75) on outsized moves
- #32: -0.2399 — relaxed outsized move but strict reversal conviction (0.25)
- #31: NOT SCORED — three-bar was too restrictive
- #6: -0.3638 — all-time champion with simpler logic

Key insight: #29 at -0.1707 is the best we have. The two-bar pattern with
strict outsized move detection (body_ratio 0.75) works well. But looking at
the distribution of scores, we're still leaving money on the table.

Hypothesis: The issue might be that we're trading ALL reversals with the same
conviction threshold, regardless of session context. A reversal that happens
after a strong session trend (measured by prior bars) might have different
staying power than one in a choppy session.

New approach: Keep #29's core logic (strict outsized detection, low reversal
threshold) but add SESSION MOMENTUM FILTERING:
1. Measure the session's momentum from the last 3 bars before the signal
2. Only trade reversals that go AGAINST the session momentum (contrarian)
3. This filters out reversals in strong trending sessions where mean reversion
   is less likely to work

Additional refinements:
- Keep move_threshold at 1.40 (proven good)
- Keep min_body_ratio at 0.75 (strict on outsized moves)
- Keep confirmation_body_ratio at 0.60 (moderate on reversal)
- Keep reversal_strength_threshold at 0.15 (low, catch all reversals)
- Add momentum_lookback of 3 bars to measure session trend
- Only enter if reversal is contrarian to session momentum
- This should filter out false reversals in strong trending moves
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.40,
        min_body_ratio: float = 0.75,
        confirmation_body_ratio: float = 0.60,
        base_size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        reversal_strength_threshold: float = 0.15,
        min_typical_range: float = 0.10,
        momentum_lookback: int = 3,
        momentum_threshold: float = 0.05,
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
        self.momentum_lookback = momentum_lookback
        self.momentum_threshold = momentum_threshold
        
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
        
        # STRICT outsized detection: range is large AND body is VERY substantial
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
        
        # TWO-BAR CONFIRMATION WITH SESSION MOMENTUM FILTER
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body
        # 3. Close beyond prior bar's open (progression)
        # 4. Show ANY reversal conviction (threshold=0.15)
        # 5. Be CONTRARIAN to session momentum (new filter)
        
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
            
            # Calculate session momentum from the last momentum_lookback bars
            # (excluding the current and previous bar which are part of our signal)
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
            
            # Check for reversal confirmation with progression and momentum filter
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                # Previous was up, current is down: check if close is below prior open
                if current_bar.close < self._bar_minus_1_open:
                    # Measure reversal strength
                    reversal_range = self._bar_minus_1_open - current_bar.low if current_bar.low < self._bar_minus_1_open else 0.1
                    reversal_distance = self._bar_minus_1_open - current_bar.close
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        # Apply momentum filter: only trade if momentum is not strongly up
                        # (trading down move is contrarian to up momentum)
                        trade_signal = True
                        if session_momentum is not None:
                            # If session momentum is strongly up, skip this down reversal
                            if session_momentum > self.momentum_threshold:
                                trade_signal = False
                        
                        if trade_signal:
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
                        # Apply momentum filter: only trade if momentum is not strongly down
                        # (trading up move is contrarian to down momentum)
                        trade_signal = True
                        if session_momentum is not None:
                            # If session momentum is strongly down, skip this up reversal
                            if session_momentum < -self.momentum_threshold:
                                trade_signal = False
                        
                        if trade_signal:
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
