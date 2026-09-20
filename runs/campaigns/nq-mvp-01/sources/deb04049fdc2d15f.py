"""
Continuation vs. mean-reversion within a session.

The baseline strategy trades every bar based on the previous close direction.
This proposal investigates whether outsized moves *continue* or *give back*.

Key changes:
1. Measure intra-session volatility (range so far this session)
2. Detect when the last bar was an outsized move relative to that volatility
3. On outsized moves, trade the continuation hypothesis: the move continues
4. Size based on the magnitude of the move to bet more on bigger signals
5. Use tighter stops/targets to capture the continuation before giveback

The logic: if a bar closes with a range that is 1.5x+ the average range so far
in this session, that's an outsized move. We buy/sell the next interval betting
on continuation, with position size and bracket levels scaled to the move size.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    def __init__(self, *, interval: str = "5m", lookback: int = 6,
                 move_threshold: float = 1.5, base_size: int = 1,
                 stop_offset: float = 0.5, target_offset: float = 1.0) -> None:
        self.interval = interval
        self.lookback = lookback  # bars to measure typical range
        self.move_threshold = move_threshold  # how many times typical range = outsized
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        self._session: int | None = None
        self._session_bars: list[Bar] = []  # track bars in current session
        self._last_outsized_direction: str | None = None  # direction of last outsized move

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Track session changes
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = []
            self._last_outsized_direction = None
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last_bar = history[-1]
        self._session_bars.append(last_bar)
        
        # If we have a position or working order, don't enter
        if ctx.position or ctx.working_order_ids:
            return
        
        # Need enough history to measure typical range
        if len(history) < self.lookback + 1:
            return
        
        # Measure typical range from the lookback window (excluding current bar)
        window = history[-(self.lookback + 1):-1]  # last lookback bars, not including current
        if len(window) < self.lookback:
            return
        
        typical_range = sum(b.high - b.low for b in window) / len(window)
        if typical_range <= 0:
            return
        
        # Check if the last bar was an outsized move
        last_range = last_bar.high - last_bar.low
        last_move = abs(last_bar.close - last_bar.open)
        
        # Outsized move: either the bar's range or the bar's directional move
        # is significantly larger than typical
        is_outsized = (last_range > typical_range * self.move_threshold or
                      last_move > typical_range * self.move_threshold)
        
        if not is_outsized:
            return
        
        # Determine direction of the outsized move
        if last_bar.close > last_bar.open:
            direction = "long"
            move_magnitude = last_bar.close - last_bar.open
        elif last_bar.close < last_bar.open:
            direction = "short"
            move_magnitude = last_bar.open - last_bar.close
        else:
            return
        
        # Store for next bar decision
        self._last_outsized_direction = direction
        
        # Scale position size based on move magnitude relative to typical range
        # Bigger moves = bigger position (up to 2x base size)
        size_multiplier = min(2.0, 1.0 + (move_magnitude / typical_range) * 0.5)
        size = max(1, int(self.base_size * size_multiplier))
        
        # Scale bracket levels: tighter on bigger moves (catch continuation faster)
        stop_mult = self.stop_offset / max(1.0, move_magnitude / typical_range)
        target_mult = self.target_offset / max(1.0, move_magnitude / typical_range)
        
        # Trade the continuation: next bar should continue the outsized move
        if direction == "long":
            ctx.buy(
                size=size,
                stop_offset=max(0.25, stop_mult),
                target_offset=max(0.5, target_mult),
                tag="continuation-long",
            )
        else:
            ctx.sell(
                size=size,
                stop_offset=max(0.25, stop_mult),
                target_offset=max(0.5, target_mult),
                tag="continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
