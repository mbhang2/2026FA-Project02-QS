"""
Momentum strategy designed to capture a planted 1-bar autocorrelation signal.
Buys when the previous bar closes up, sells when it closes down.
Uses an asymmetric bracket (target > stop) to achieve a positive expected value
if the signal provides directional edge.
"""

class Strategy:
    def __init__(self, interval="5m", stop_offset=0.75, target_offset=1.5, size=1):
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="mom-long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="mom-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
