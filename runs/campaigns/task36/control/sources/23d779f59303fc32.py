"""
Momentum strategy capturing consecutive closes in the same direction.
Waits for two bars of agreement to enter, and relies on the engine's 
bracket orders for safe exits.
"""

class Strategy:
    def __init__(self, interval="5m", size=1, stop_offset=1.5, target_offset=3.0):
        self.interval = interval
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        prev, last = history[-2], history[-1]
        
        up_momentum = last.close > last.open and prev.close > prev.open
        down_momentum = last.close < last.open and prev.close < prev.open

        if up_momentum:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="mom-long"
            )
        elif down_momentum:
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
