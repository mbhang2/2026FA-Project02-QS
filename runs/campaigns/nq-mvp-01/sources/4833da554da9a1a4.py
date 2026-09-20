"""
Mean-reversion fade with simplified two-bar pattern and tighter filters.

Analysis of recent trials:
- #42: -0.0034 — best so far with only 2 trades (volatility filter working)
- #41: -0.0483 — regression from adaptive stop/target
- #39: -0.0304 — good baseline with simple two-bar pattern

Key insight: #42 achieved the best score with extremely few trades (2), suggesting
that the volatility/range stability filter is the critical insight. The strategy
is filtering out most of the noise and only trading in the cleanest conditions.

However, 2 trades is suspiciously low - we may be over-filtering. The regression
from #41 to #42 suggests that adding complexity (penetration checks, range_cv
threshold) actually helped despite fewer trades.

Hypothesis for #43: Keep #42's core approach but make one critical refinement:
The range_stability_threshold (0.25) may be too strict. Let's relax it slightly
to 0.35 to allow more trades while maintaining quality. Also, simplify the
momentum logic - the adaptive threshold adjustment wasn't helping.

Key changes from #42:
1. Increase range_stability_threshold from 0.25 to 0.35 (more permissive)
2. Remove the adaptive momentum threshold adjustment - just use binary filter
3. Keep all the good stuff: penetration checks, range stability concept, 
   stricter confirmation criteria
4. Keep the exact same stop/target offsets (0.70/0.72) that worked in #39/#42

This should generate more trades than #42 while maintaining the quality filters
that made it the best performer.
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
        range_stability_threshold: float = 0.35,
        min_confirmation_penetration: float = 0.50,
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
        self.range_stability_threshold = range_stability_threshold
        self.min_confirmation_penetration = min_confirmation_penetration
        
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
        
        # TWO-BAR CONFIRMATION WITH STRICTER CRITERIA
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
            
            # Check for reversal confirmation with stricter criteria
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                # Previous was up, current is down
                if current_bar.close < self._bar_minus_1_open:
                    # Measure reversal strength - how far current penetrates into prior range
                    reversal_range = self._bar_minus_1_high - self._bar_minus_1_open if self._bar_minus_1_high > self._bar_minus_1_open else 0.1
                    reversal_distance = self._bar_minus_1_open - current_bar.close
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    # Check how much current bar penetrates back into prior bar's range
                    prior_range = self._bar_minus_1_high - self._bar_minus_1_low
                    penetration = (self._bar_minus_1_open - current_bar.close) / prior_range if prior_range > 0 else 0
                    
                    # Require meaningful penetration into the prior bar's range
                    if penetration >= self.min_confirmation_penetration and reversal_strength >= self.reversal_strength_threshold:
                        # Simple momentum filter: skip if momentum is strongly up
                        trade_signal = True
                        if session_momentum is not None:
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
                # Previous was down, current is up
                if current_bar.close > self._bar_minus_1_open:
                    # Measure reversal strength
                    reversal_range = self._bar_minus_1_open - self._bar_minus_1_low if self._bar_minus_1_open > self._bar_minus_1_low else 0.1
                    reversal_distance = current_bar.close - self._bar_minus_1_open
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    # Check how much current bar penetrates back into prior bar's range
                    prior_range = self._bar_minus_1_high - self._bar_minus_1_low
                    penetration = (current_bar.close - self._bar_minus_1_open) / prior_range if prior_range > 0 else 0
                    
                    # Require meaningful penetration into the prior bar's range
                    if penetration >= self.min_confirmation_penetration and reversal_strength >= self.reversal_strength_threshold:
                        # Simple momentum filter: skip if momentum is strongly down
                        trade_signal = True
                        if session_momentum is not None:
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
        self._bar_minus_1_high = current_bar.high
        self._bar_minus_1_low = current_bar.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
