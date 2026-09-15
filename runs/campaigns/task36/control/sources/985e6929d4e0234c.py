"""Volatility-scaled momentum strategy.

Improves upon the baseline 1-bar momentum by scaling the bracket offsets
according to the signal bar's true range. This allows profits to run further
on strong momentum while maintaining a proportional stop, capturing the 
planted autoregressive signal with a better risk-reward asymmetry.
"""

class Strategy:
    def __init__(self, interval="5m", size=1, stop_mult=1.0, target_mult=2.0, min_offset=0.75):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_offset = min_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        if last.close == last.open:
            return
            
        # Scale bracket by the bar's range, floored to avoid noise stop-outs
        rng = max(last.high - last.low, self.min_offset)
        
        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=rng * self.stop_mult,
                target_offset=rng * self.target_mult,
                tag="long"
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=rng * self.stop_mult,
                target_offset=rng * self.target_mult,
                tag="short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
