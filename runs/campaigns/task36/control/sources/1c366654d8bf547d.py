"""
A simple Moving Average Crossover strategy.

Subscribes to 5m bars, waits for the fast MA to cross the slow MA, and enters a position
with a protective stop and target bracket. It uses the history window correctly and 
keeps all state management within the provided context.
"""

from __future__ import annotations

class Strategy:
    def __init__(
        self,
        fast: int = 10,
        slow: int = 30,
        interval: str = "5m",
        size: int = 1,
        stop_offset: float = 2.0,
        target_offset: float = 4.0
    ):
        self.fast = fast
        self.slow = slow
        self.interval = interval
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Only act if we are actionable and flat with no working orders
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        # Check if we have enough bars to compute the slow MA and its previous value
        if len(history) < self.slow + 1:
            return
            
        # Fetch only the window we need to respect the history window limit
        window = history.last(self.slow + 1)
        closes = [b.close for b in window]
        
        # Current MAs (including the bar that just closed)
        fast_ma_curr = sum(closes[-self.fast:]) / self.fast
        slow_ma_curr = sum(closes[-self.slow:]) / self.slow
        
        # Previous MAs (excluding the bar that just closed)
        fast_ma_prev = sum(closes[-self.fast-1:-1]) / self.fast
        slow_ma_prev = sum(closes[-self.slow-1:-1]) / self.slow
        
        # Crossover logic
        if fast_ma_prev <= slow_ma_prev and fast_ma_curr > slow_ma_curr:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="ma-long"
            )
        elif fast_ma_prev >= slow_ma_prev and fast_ma_curr < slow_ma_curr:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="ma-short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
