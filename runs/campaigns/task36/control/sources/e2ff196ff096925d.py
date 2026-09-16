"""Continuous momentum strategy capturing the planted signal.

The baseline strategy is 'dumb' because it uses a fixed bracket and refuses
to act while a position is open, causing it to miss consecutive trend bars.
This strategy holds the position as long as the trend continues and reverses
when the signal flips, maximizing time in the market aligned with the planted signal.
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
            
        diff = target - ctx.position
        if diff == 0:
            return
            
        if ctx.working_order_ids:
            ctx.cancel_all()
            
        if diff > 0:
            ctx.buy(size=diff, tag="reverse_long")
        elif diff < 0:
            ctx.sell(size=-diff, tag="reverse_short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
