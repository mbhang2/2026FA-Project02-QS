"""
Breakout strategy over recent bars to capture planted momentum.
Uses a short lookback and bracket sizing based on the recent range.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=4, size=1, stop_mult=1.0, target_mult=2.0):
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
        last = window[-1]
        prior = window[:-1]
        
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        
        # Ensure a minimum offset if the range is extremely tight or zero
        stop = max(rng * self.stop_mult, 0.25)
        target = max(rng * self.target_mult, 0.5)

        if last.close > high:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target, tag="breakout-long")
        elif last.close < low:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target, tag="breakout-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
