"""
Continuous momentum strategy that maximizes exposure to the planted signal.
By removing the tight brackets of the baseline strategy, this version holds the 
position for the full interval, capturing the complete 1-bar momentum effect.
"""

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
            desired = self.size
        elif last.close < last.open:
            desired = -self.size
        else:
            desired = 0
            
        current = ctx.position
        
        if current == desired:
            return
            
        if ctx.working_order_ids:
            ctx.cancel_all()
            
        diff = desired - current
        if diff > 0:
            ctx.buy(size=diff, tag="long")
        elif diff < 0:
            ctx.sell(size=-diff, tag="short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
