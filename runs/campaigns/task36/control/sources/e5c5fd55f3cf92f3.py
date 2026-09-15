"""Momentum strategy that trades in the direction of the last closed bar,
filtering for bars where the body is a significant portion of the range,
and using dynamic brackets for stops and targets based on the bar's range.
This aims to capture the planted 1-bar momentum signal more effectively
by avoiding weak signals and adapting to volatility.
"""

class Strategy:
    def __init__(self, interval="5m", size=1, stop_mult=1.0, target_mult=2.0, body_frac=0.5):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.body_frac = body_frac

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        rng = last.high - last.low
        if rng <= 0:
            return
            
        body = abs(last.close - last.open)
        if body / rng < self.body_frac:
            return
            
        stop = max(0.25, rng * self.stop_mult)
        target = max(0.25, rng * self.target_mult)
        
        if last.close > last.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target, tag="long")
        elif last.close < last.open:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target, tag="short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
