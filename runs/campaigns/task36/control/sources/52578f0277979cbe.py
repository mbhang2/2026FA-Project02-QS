"""Continuous Momentum Strategy.

The placeholder is described as the "dumbest" way to express the planted signal 
because it uses fixed bracket exits and only enters when flat. This leads to 
over-trading, premature stop-outs, and missing the continuation of the trend. 

This strategy improves on it by maintaining a continuous position in the direction
of the last closed bar, reversing only when the signal flips. This captures the
full auto-correlated trend while minimizing commission costs by avoiding unnecessary
turnover.
"""

class Strategy:
    def __init__(self, interval="5m", size=1):
        self.interval = interval
        self.size = size

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return
            
        if ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        if last.close > last.open:
            target_pos = self.size
        elif last.close < last.open:
            target_pos = -self.size
        else:
            return
            
        current_pos = ctx.position
        
        if target_pos > current_pos:
            ctx.buy(size=target_pos - current_pos, tag="long")
        elif target_pos < current_pos:
            ctx.sell(size=current_pos - target_pos, tag="short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
