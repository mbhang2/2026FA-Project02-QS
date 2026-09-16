"""
Simple momentum strategy that enters based on the last closed bar's direction.
Uses wider bracket offsets than the baseline to reduce turnover and commission drag,
allowing the planted signal to run further before exiting.
"""

class Strategy:
    def __init__(self, interval: str = "5m", stop: float = 2.0, target: float = 4.0, size: int = 1):
        self.interval = interval
        self.stop = stop
        self.target = target
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

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
