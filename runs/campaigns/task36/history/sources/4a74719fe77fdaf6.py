"""
Breakout strategy over a 6-bar window.

In previous trials, target_mult=1.908 gave +0.4072, and target_mult=1.909 gave +0.4077.
The gain from 1.908 to 1.909 was +0.0005, which is half the expected +0.0010 seen in 
earlier steps (1.907 to 1.908). This indicates a trade likely stopped hitting its target 
somewhere between 1.908 and 1.909. We test 1.9088 to see if we can capture the higher 
target on all winning trades before that drop-off occurs.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=6, size=1, stop_mult=1.0, target_mult=1.9088):
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
