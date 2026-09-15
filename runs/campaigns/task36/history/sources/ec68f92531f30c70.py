"""
Breakout strategy over a 6-bar window.

We have extensively optimized target_mult (around 1.909090875) with stop_mult=1.0, 
achieving a peak score of +0.4078 (91 trades).
In the previous trial, stop_mult=1.01 also scored +0.4078 (91 trades), showing a 
plateau. Here we test stop_mult=1.05 to find where the performance begins to drop 
between 1.01 and 1.20 (which scored +0.0388).
"""

class Strategy:
    def __init__(self, interval="5m", lookback=6, size=1, stop_mult=1.05, target_mult=1.909090875):
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
