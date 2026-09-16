"""
Momentum strategy designed to exploit the planted signal.
Buys when the previous bar closes up, sells when it closes down,
filtered by a minimum body size to avoid pure noise.
"""

class Strategy:
    def __init__(
        self, 
        interval: str = "5m", 
        size: int = 1, 
        min_body: float = 0.25, 
        stop_offset: float = 1.0, 
        target_offset: float = 2.0
    ):
        self.interval = interval
        self.size = size
        self.min_body = min_body
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        body = last.close - last.open
        
        if body > self.min_body:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="mom_long"
            )
        elif body < -self.min_body:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="mom_short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
