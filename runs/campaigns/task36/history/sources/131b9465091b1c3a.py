"""
Breakout strategy over a 5-bar window.

In previous trials, lookback=6 was heavily optimized to a peak score of +0.4078.
lookback=7 gave +0.1201.
Here we test lookback=5 to see if a shorter window provides a better breakout signal.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=5, size=1, stop_mult=1.0, target_mult=1.90909):
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
