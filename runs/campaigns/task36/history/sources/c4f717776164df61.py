"""
Breakout strategy over a 6-bar window.

In previous trials, target_mult was optimized around 1.909 for a score of +0.4077.
We also found that loosening the stop to 1.05 didn't change the score.
Now we will test whether a tighter stop (stop_mult=0.9) improves the score by 
cutting losses earlier on trades that eventually stop out, without prematurely 
stopping out the winning trades.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=6, size=1, stop_mult=0.9, target_mult=1.909):
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
