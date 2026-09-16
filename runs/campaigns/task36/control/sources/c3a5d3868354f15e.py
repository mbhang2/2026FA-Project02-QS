"""Continuous momentum strategy.

Reverses position based on the direction of the last closed bar,
staying in the market continuously to maximize exposure to the planted signal
rather than exiting early via a bracket.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", size: int = 1):
        self.interval = interval
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
            
        if ctx.working_order_ids:
            ctx.cancel_all()
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        if last.close > last.open:
            target = self.size
        elif last.close < last.open:
            target = -self.size
        else:
            target = ctx.position
            
        current = ctx.position
        
        if target > current:
            ctx.buy(size=target - current, tag="go-long")
        elif target < current:
            ctx.sell(size=current - target, tag="go-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
