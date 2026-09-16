"""
Breakout strategy over a 6-bar window.

Builds on the successful breakout strategy (Trial #19) which scored +0.4031
using lookback=6, stop_mult=1.0, target_mult=1.9.
Here we test target_mult=1.95 to fine-tune the profit taking level between 1.9 and 2.0
(which scored +0.3061), aiming for the absolute peak of the parameter surface.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=6, size=1, stop_mult=1.0, target_mult=1.95):
        self.interval = interval
        self.lookback = lookback
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        last, prior = window[-1], window[:-1]
        
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if last.close > high:
            ctx.buy(
                size=self.size, 
                stop_offset=stop, 
                target_offset=target, 
                tag="breakout-long"
            )
        elif last.close < low:
            ctx.sell(
                size=self.size, 
                stop_offset=stop, 
                target_offset=target, 
                tag="breakout-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
