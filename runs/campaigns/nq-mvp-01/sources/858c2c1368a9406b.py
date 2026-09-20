"""
Mean-reversion fade with adaptive body ratio filtering.

Analysis of recent trials:
- #29: -0.1707 — best recent, uses strict body ratio (0.75) on outsized moves
- #6: -0.3638 — all-time champion, simpler logic with body ratio 0.70
- #28: -0.1903 — three-bar confirmation didn't help
- #27: -0.1954 — reversal strength threshold 0.40 was too strict

The puzzle: #6 with body_ratio=0.70 scores -0.3638, but #29 with body_ratio=0.75
scores -0.1707 (worse). This suggests:
1. The stricter filter removes too many good trades
2. Or the interaction with other parameters matters

Key observation: #6's success came from simplicity and the right balance of
selectivity. The two-bar pattern (outsized + reversal) is real, but we're
filtering too aggressively on the outsized move itself.

New hypothesis: Instead of a FIXED strict body ratio, use ADAPTIVE filtering:
- Measure the recent volatility context (last 8 bars)
- Scale the body ratio requirement based on how "noisy" the session is
- Quieter sessions: require higher body ratio (filter more aggressively)
- Noisier sessions: accept lower body ratio (let more trades through)
- This way we're selecting based on RELATIVE signal strength, not absolute

This keeps the core two-bar pattern but makes it adaptive to market conditions,
which might capture more of the edge that #6 found while still being selective.

Additional refinement: Lower the reversal_strength_threshold slightly (0.12)
to match what the best performers used, while keeping the adaptive body ratio
as the main filter.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.40,
        base_body_ratio: float = 0.70,
        volatility_window: int = 8,
        confirmation_body_ratio: float = 0.60,
        base_size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        reversal_strength_threshold: float = 0.12,
        min_typical_range: float = 0.10,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.base_body_ratio = base_body_ratio
        self.volatility_window = volatility_window
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.reversal_strength_threshold = reversal_strength_threshold
        self.min_typical_range = min_typical_range
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._typical_range: float = 0.0
        self._recent_bodies: list[float] = []

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
            self._recent_bodies = []
        
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
        
        # Track recent bar bodies for volatility adaptation
        current_body = abs(current_bar.close - current_bar.open)
        self._recent_bodies.append(current_body)
        if len(self._recent_bodies) > self.volatility_window:
            self._recent_bodies.pop(0)
        
        # Compute adaptive body ratio based on recent volatility
        if len(self._recent_bodies) >= self.volatility_window:
            avg_body = sum(self._recent_bodies) / len(self._recent_bodies)
            body_std = (sum((b - avg_body) ** 2 for b in self._recent_bodies) / len(self._recent_bodies)) ** 0.5
            
            # Volatility coefficient: higher volatility -> lower required body ratio
            # Normalize by typical range to get a relative measure
            volatility_coeff = min(1.5, max(0.7, 1.0 - (body_std / typical_range) * 0.3))
            adaptive_body_ratio = self.base_body_ratio * volatility_coeff
        else:
            adaptive_body_ratio = self.base_body_ratio
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        
        # Outsized detection: range is large AND body meets adaptive threshold
        is_outsized = (
            current_range > typical_range * self.move_threshold and
            current_body > current_range * adaptive_body_ratio
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
        # 4. Show sufficient reversal strength
        
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
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
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_open = current_open

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
