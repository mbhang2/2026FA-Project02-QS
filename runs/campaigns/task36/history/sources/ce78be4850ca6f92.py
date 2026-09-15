"""
Breakout strategy over a 6-bar window.

In Trial #32, target_mult=1.909 gave our peak score of +0.4077 with 91 trades.
In Trial #36, reducing stop_mult to 0.99 caused a sharp drop in performance (score +0.0431, 102 trades), 
indicating that a stop_mult of 0.99 is too tight and changes the trade sequence negatively.
Here we test stop_mult=0.999 to see if we can slightly reduce losses on losing trades 
without altering the favorable 91-trade sequence.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=6, size=1, stop_mult=0.999, target_mult=1.909):
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
