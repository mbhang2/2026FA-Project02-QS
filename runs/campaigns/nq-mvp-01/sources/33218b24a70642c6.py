"""
Mean-reversion fade with adaptive volatility filtering and mid-session focus.

Analysis of recent trials:
- #46: +0.0002 — 2 trades (too selective, early_session_bars=6 too restrictive)
- #44: +0.0256 — 3 trades, BEST score (high conviction works)
- #45: +0.0002 — 6 trades (relaxation degraded score)
- #43: -0.0495 — 9 trades (too permissive)

Key insight: #44 achieved +0.0256 with 3 high-conviction trades. #46's attempt
to be more selective (early_session_bars=6) dropped to only 2 trades, suggesting
we're now TOO restrictive. The sweet spot appears to be around 4-5 bars before
entering, not 6.

The pattern that works:
1. Wait for session to establish (4-5 bars, not 6)
2. Detect outsized moves (1.32x typical range with strong body)
3. Confirm reversal on next bar (strong counter-direction body)
4. Fade only when momentum is weak (critical filter)
5. Exit with tight brackets (0.70/0.72)

Hypothesis for #47: The issue with #46 was TOO MUCH patience. We need to:

1. REDUCE early_session_bars from 6 back to 4 (let's catch mid-morning trades)
2. RELAX reversal_strength_threshold from 0.23 to 0.21 (allow slightly weaker
   reversals since we're trading more mid-session)
3. ADJUST momentum_threshold from 0.08 to 0.10 (slightly more permissive, but
   still strict about trading against strong momentum)
4. KEEP range_stability_threshold at 0.22 (this filter works well)
5. KEEP base_confirmation_body_ratio at 0.60 (quality confirmation matters)
6. TRY move_threshold at 1.28 (slightly lower to catch more opportunities
   without degrading quality)

The goal: Find the 4-6 high-quality trades per run that are profitable, by
balancing patience (not trading too early) with opportunity (not waiting too
long). #44's 3 trades suggests the pattern exists; we just need to find more
instances of it.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.28,
        min_body_ratio: float = 0.75,
        base_confirmation_body_ratio: float = 0.60,
        base_size: int = 1,
        stop_offset: float = 0.70,
        target_offset: float = 0.72,
        reversal_strength_threshold: float = 0.21,
        min_typical_range: float = 0.10,
        momentum_lookback: int = 4,
        momentum_threshold: float = 0.10,
        early_session_bars: int = 4,
        late_session_bars: int = 4,
        range_stability_threshold: float = 0.22,
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
