"""
Momentum strategy to capture the planted signal.
Buys if the last bar closed up, sells if it closed down.
Uses a bracket order to protect the position.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", stop_offset: float = 1.0,
                 target_offset: float = 2.0, size: int = 1):
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
            
        if ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        if last.close > last.open:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="long"
            )
            if order_id is None:
                pass
        elif last.close < last.open:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="short"
            )
            if order_id is None:
                pass

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
