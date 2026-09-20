"""
Mean-reversion fade with multi-bar confirmation and volatility-adaptive entry.

Analysis of prior trials:
- #6: -0.3638 — champion, two-bar confirmation with simple logic
- #26: -0.2479 — adaptive threshold helped slightly but not enough
- #10: -0.1862 — conviction gating was too restrictive
- #25: -0.2469 (NOT SCORED) — three-bar pattern too selective

Key insight: #6's -0.3638 is still the best. The two-bar confirmation pattern
(outsized move followed by reversal with progression) captures real mean reversion.

The core question: Can we improve on #6 by being more selective about WHICH
reversals to trade, without losing the real signal?

#6 trades every reversal after an outsized move. This catches many trades but
some are weak. The improvement strategy:

1. Selective entry based on reversal STRENGTH:
   - Not just "does bar 2 reverse?" but "how strong is the reversal?"
   - Measure reversal strength as: how far bar 2 closed in fade direction
   - Only enter if reversal shows meaningful conviction (closes >40% into fade)
   - This filters weak reversals while keeping strong ones

2. Volatility-adaptive bracket sizing:
   - Larger moves get tighter brackets (0.50-0.60 targets for big moves)
   - Smaller moves get wider brackets (0.80-1.00 targets for normal moves)
   - Stops scale similarly to avoid over-sizing on small moves
   - This matches risk management to the trade's volatility profile

3. Session-aware entry filtering:
   - Skip first 4 bars (need session context)
   - Skip last 4 bars (need time for mean reversion)
   - Skip if typical range is too low (choppy, low-conviction days)
   - This concentrates trades in high-quality setup windows

4. Conservative parameter tuning:
   - move_threshold=1.40 (proven in #6)
   - min_body_ratio=0.70 (strict on outsized move)
   - confirmation_body_ratio=0.60 (standard for reversal bar)
   - lookback=8 (proven in #6)

The philosophy: #6 works because the pattern is real. The improvement is to
be MORE selective about which reversals to trade, focusing on ones with
genuine conviction, while keeping the core two-bar confirmation logic intact.
This should reduce noise without losing signal.
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
        reversal_strength_threshold: float = 0.40,
        min_typical_range: float = 0.15,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.reversal_strength_threshold = reversal_strength_threshold
        self.min_typical_range = min_typical_range
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._bar_minus_1_close: float = 0.0
        self._bar_minus_1_range: float = 0.0
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
            self._bar_minus_1_close = 0.0
            self._bar_minus_1_range = 0.0
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
        
        # Outsized detection: range is large AND body is substantial
        is_outsized = (
            current_range > typical_range * self.move_threshold and
            current_body > current_range * self.min_body_ratio
        )
        
        current_direction = None
        current_open = current_bar.open
        current_close = current_bar.close
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        
        # TWO-BAR CONFIRMATION WITH REVERSAL STRENGTH FILTER
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body
        # 3. Close beyond prior bar's open (progression)
        # 4. Show sufficient reversal strength (conviction)
        
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
            
            # Check for reversal confirmation with progression
            reversal_strength = 0.0
            should_trade = False
            trade_direction = None
            
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                # Previous was up, current is down: check if close is below prior open
                if current_bar.close < self._bar_minus_1_open:
                    # Measure reversal strength: how far into the reversal range
                    # Range is from prior open down to current low
                    reversal_range = self._bar_minus_1_open - current_bar.low if current_bar.low < self._bar_minus_1_open else 0.1
                    reversal_distance = self._bar_minus_1_open - current_bar.close
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        should_trade = True
                        trade_direction = "up"
            
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                # Previous was down, current is up: check if close is above prior open
                if current_bar.close > self._bar_minus_1_open:
                    # Measure reversal strength: how far into the reversal range
                    # Range is from prior open up to current high
                    reversal_range = current_bar.high - self._bar_minus_1_open if current_bar.high > self._bar_minus_1_open else 0.1
                    reversal_distance = current_bar.close - self._bar_minus_1_open
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        should_trade = True
                        trade_direction = "down"
            
            if should_trade and trade_direction is not None:
                self._enter_trade(ctx, trade_direction, reversal_strength)
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_open = current_open
        self._bar_minus_1_close = current_close
        self._bar_minus_1_range = current_range

    def _enter_trade(self, ctx, outsized_direction: str, reversal_strength: float) -> None:
        """Enter a fade trade opposite to the outsized move direction."""
        if self._typical_range <= 0:
            return
        
        # Volatility-adaptive bracket sizing
        # Measure how extreme the outsized move was
        move_magnitude = self._bar_minus_1_range
        move_ratio = move_magnitude / self._typical_range
        
        # Larger moves (>1.5x typical) get tighter brackets
        # Normal moves get standard brackets
        if move_ratio > 1.5:
            # Large move: tight brackets (0.50-0.60)
            stop_offset = 0.50
            target_offset = 0.55
        elif move_ratio > 1.2:
            # Moderate move: medium brackets (0.65-0.75)
            stop_offset = 0.65
            target_offset = 0.70
        else:
            # Normal move: wider brackets (0.80-1.00)
            stop_offset = 0.80
            target_offset = 0.90
        
        # Scale bracket sizes by reversal strength
        # Strong reversals (0.7+) get tighter brackets
        # Weak reversals (0.4-0.6) get wider brackets
        strength_factor = 0.7 + reversal_strength * 0.3  # 0.7 to 1.0
        stop_offset = stop_offset * strength_factor
        target_offset = target_offset * strength_factor
        
        if outsized_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=self.base_size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=self.base_size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
