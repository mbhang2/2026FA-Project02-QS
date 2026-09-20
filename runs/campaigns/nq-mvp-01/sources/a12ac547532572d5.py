"""
Mean-reversion within a session after outsized moves.

The previous strategy tested continuation after outsized moves and scored -0.7271.
This proposal inverts the hypothesis: outsized moves give back, not continue.

Key changes:
1. Detect outsized moves (bars with range > 1.5x typical session range)
2. Trade mean-reversion: fade the outsized move in the next bar
3. Enter opposite to the outsized move direction
4. Use wider stops/targets to capture the reversion before continuation resumes
5. Size based on move magnitude - bigger moves should revert more

The logic: when a bar makes an outsized move (>1.5x typical range), it represents
a temporary imbalance. The next bars should revert toward the session mean. We
fade the move by trading opposite to its direction, betting on mean-reversion.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", lookback: int = 6,
                 move_threshold: float = 1.5, base_size: int = 1,
                 stop_offset: float = 1.0, target_offset: float = 0.5) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        self._session = None
        self._session_high = None
        self._session_low = None
        self._session_bars_count = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Reset on new session
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_high = None
            self._session_low = None
            self._session_bars_count = 0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last_bar = history[-1]
        
        # Track session high/low for context
        if self._session_high is None:
            self._session_high = last_bar.high
            self._session_low = last_bar.low
        else:
            self._session_high = max(self._session_high, last_bar.high)
            self._session_low = min(self._session_low, last_bar.low)
        
        self._session_bars_count += 1
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Need enough history to measure typical range
        if len(history) < self.lookback + 1:
            return
        
        # Measure typical range from the lookback window (excluding current bar)
        window = history[-(self.lookback + 1):-1]
        if len(window) < self.lookback:
            return
        
        typical_range = sum(b.high - b.low for b in window) / len(window)
        if typical_range <= 0:
            return
        
        # Check if the last bar was an outsized move
        last_range = last_bar.high - last_bar.low
        last_move = abs(last_bar.close - last_bar.open)
        
        # Outsized move detection
        is_outsized = (last_range > typical_range * self.move_threshold or
                      last_move > typical_range * self.move_threshold)
        
        if not is_outsized:
            return
        
        # Determine direction of the outsized move
        if last_bar.close > last_bar.open:
            # Outsized up move - fade it by selling
            move_direction = "up"
            move_magnitude = last_bar.close - last_bar.open
        elif last_bar.close < last_bar.open:
            # Outsized down move - fade it by buying
            move_direction = "down"
            move_magnitude = last_bar.open - last_bar.close
        else:
            return
        
        # Scale position size based on move magnitude
        # Bigger moves = bigger position (expect bigger reversion)
        size_multiplier = min(2.0, 1.0 + (move_magnitude / typical_range) * 0.5)
        size = max(1, int(self.base_size * size_multiplier))
        
        # Scale bracket levels: wider on bigger moves (reversion takes longer)
        stop_mult = self.stop_offset * max(1.0, move_magnitude / typical_range)
        target_mult = self.target_offset * max(0.5, move_magnitude / typical_range)
        
        # Fade the outsized move (trade opposite direction)
        if move_direction == "up":
            # Sell to fade the up move
            ctx.sell(
                size=size,
                stop_offset=max(0.5, stop_mult),
                target_offset=max(0.25, target_mult),
                tag="fade-up",
            )
        else:
            # Buy to fade the down move
            ctx.buy(
                size=size,
                stop_offset=max(0.5, stop_mult),
                target_offset=max(0.25, target_mult),
                tag="fade-down",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
