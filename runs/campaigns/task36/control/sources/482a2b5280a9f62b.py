"""
A momentum strategy designed to capture the planted signal.
Buys when the last bar closes up, sells when it closes down,
using a wider bracket to avoid premature stop-outs from noise.
"""

class Strategy:
    def __init__(self, interval="5m", stop_offset=2.0, target_offset=4.0, size=1):
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
                tag="entry-long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="entry-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
