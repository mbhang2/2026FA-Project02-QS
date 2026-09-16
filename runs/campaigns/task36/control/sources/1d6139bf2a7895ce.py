"""1-bar momentum strategy with dynamic brackets.

Follows the planted 1-bar momentum signal but sizes the protective bracket
dynamically based on the previous bar's range, ensuring stops and targets
adapt to local volatility.
"""

class Strategy:
    def __init__(self, interval="5m", size=1, stop_mult=1.0, target_mult=2.0, min_offset=0.5):
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

        rng = last.high - last.low
        stop = max(rng * self.stop_mult, self.min_offset)
        target = max(rng * self.target_mult, self.min_offset)

        if last.close > last.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target, tag="mom-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target, tag="mom-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
