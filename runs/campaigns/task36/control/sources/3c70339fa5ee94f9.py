"""
One-bar momentum strategy with asymmetric bracket.
Buys if the last bar was up, sells if the last bar was down.
Uses an asymmetric bracket order to manage the trade, aiming for a higher target.
"""

class Strategy:
    def __init__(self, interval: str = "5m", stop: float = 1.0, target: float = 2.0, size: int = 1):
        self.interval = interval
        self.stop = stop
        self.target = target
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        if last.close > last.open:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop, 
                target_offset=self.target, 
                tag="long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop, 
                target_offset=self.target, 
                tag="short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
