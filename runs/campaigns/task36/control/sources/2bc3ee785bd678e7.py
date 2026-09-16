"""
Continuous momentum strategy.
Exploits the planted signal by holding the position for the full interval,
reversing only when the signal changes. This maximizes exposure to the 
autocorrelation while minimizing commission costs compared to a bracketed exit.
"""

class Strategy:
    def __init__(self, interval: str = "5m", size: int = 1):
        self.interval = interval
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.working_order_ids:
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
            desired = ctx.position
            
        current = ctx.position
        
        if desired > current:
            ctx.buy(size=desired - current)
        elif desired < current:
            ctx.sell(size=current - desired)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
