"""
Mean-reversion fade with ultra-selective entry and adaptive session timing.

Analysis of recent trials:
- #44: +0.0256 — 3 trades, BEST SCORE (ultra-selective works)
- #48: NOT SCORED — 0 trades (too strict, missed opportunities)
- #47: -0.0034 — 2 trades (move_threshold=1.28 let in noise)
- #46: +0.0002 — 2 trades (early_session_bars=6 too restrictive)

Key insight: #44 achieved +0.0256 with 3 perfect trades. #48's attempt to be
even stricter (move_threshold=1.35, momentum_threshold=0.06) resulted in 0 trades
— we overshot the selectivity sweet spot.

The winning pattern from #44 works because it balances:
1. Patience: Wait 3-4 bars for session context
2. Conviction: Only trade obvious outsized moves (1.32x range)
3. Confirmation: Require strong reversal with good body
4. Momentum: Fade only when momentum is weak/neutral
5. Execution: Tight brackets (0.70/0.72)

Hypothesis for #49: #48 was too extreme. We need to dial back slightly from
#48's ultra-strict parameters while keeping the core discipline:

1. MODERATE move_threshold: 1.30 (between #44's 1.32 and #48's 1.35, catching
   clear outsized moves without being too restrictive)
2. MODERATE reversal_strength_threshold: 0.23 (between #44's 0.25 and #48's 0.26,
   requiring strong but not extreme reversals)
3. MODERATE momentum_threshold: 0.08 (between #44's 0.10 and #48's 0.06, strict
   but not paralyzing)
4. MODERATE confirmation body ratio: 0.62 (between #44's 0.58 and #48's 0.65,
   requiring good quality confirmation)
5. EARLY entry: Keep early_session_bars at 3 (catch mid-morning trades)
6. BALANCED range stability: 0.21 (slightly looser than #48's 0.20 to allow more
   sessions, tighter than #44's 0.25)
7. ADAPTIVE momentum lookback: 5 bars (balance between #44's 4 and #48's 6)

The goal: Find 4-6 high-quality trades per run (more than #44's 3, fewer than
#43's 9) by being selective but not paralyzing. We're looking for the "Goldilocks
zone" where we get consistent profitable trades without over-filtering.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.30,
        min_body_ratio: float = 0.75,
        base_confirmation_body_ratio: float = 0.62,
        base_size: int = 1,
        stop_offset: float = 0.70,
        target_offset: float = 0.72,
        reversal_strength_threshold: float = 0.23,
        min_typical_range: float = 0.10,
        momentum_lookback: int = 5,
        momentum_threshold: float = 0.08,
        early_session_bars: int = 3,
        late_session_bars: int = 4,
        range_stability_threshold: float = 0.21,
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
        self.range_stability_threshold = range_stability_threshold
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._bar_minus_1_high: float = 0.0
        self._bar_minus_1_low: float = 0.0
        self._bar_minus_1_range: float = 0.0

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
            self._bar_minus_1_range = 0.0
        
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
        
        ranges = [b.high - b.low for b in window]
        typical_range = sum(ranges) / len(ranges)
        if typical_range < self.min_typical_range:
            return
        
        # Check range stability - don't trade if volatility is too inconsistent
        avg_range = typical_range
        range_std = (sum((r - avg_range) ** 2 for r in ranges) / len(ranges)) ** 0.5
        if avg_range > 0:
            range_cv = range_std / avg_range
            if range_cv > self.range_stability_threshold:
                # Volatility too unstable, skip
                self._bar_minus_1_outsized = False
                self._bar_minus_1_direction = None
                return
        
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
        
        # TWO-BAR CONFIRMATION PATTERN
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # Check body quality of confirmation bar
            has_good_body = (
                current_body > current_range * self.base_confirmation_body_ratio
                if current_range > 0 else False
            )
            
            # Calculate session momentum from the lookback window
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
                    prior_range = self._bar_minus_1_high - self._bar_minus_1_low
                    if prior_range > 0:
                        reversal_distance = self._bar_minus_1_open - current_bar.close
                        reversal_strength = reversal_distance / prior_range
                        
                        if reversal_strength >= self.reversal_strength_threshold:
                            # Momentum filter: only trade if momentum is weak or neutral
                            trade_signal = True
                            if session_momentum is not None:
                                if abs(session_momentum) > self.momentum_threshold:
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
                # Previous was down, current is up
                if current_bar.close > self._bar_minus_1_open:
                    # Measure reversal strength
                    prior_range = self._bar_minus_1_high - self._bar_minus_1_low
                    if prior_range > 0:
                        reversal_distance = current_bar.close - self._bar_minus_1_open
                        reversal_strength = reversal_distance / prior_range
                        
                        if reversal_strength >= self.reversal_strength_threshold:
                            # Momentum filter: only trade if momentum is weak or neutral
                            trade_signal = True
                            if session_momentum is not None:
                                if abs(session_momentum) > self.momentum_threshold:
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
        self._bar_minus_1_high = current_bar.high
        self._bar_minus_1_low = current_bar.low
        self._bar_minus_1_range = current_bar.high - current_bar.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
