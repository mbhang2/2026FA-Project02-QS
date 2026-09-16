"""Momentum strategy with volatility-adjusted brackets.

Builds on the baseline 1-bar momentum signal (buy if the last bar closed up,
sell if it closed down), but dynamically sizes the protective stop and profit
target based on the range of that signal bar. This allows the strategy to give
trades more room to breathe during volatile periods and tighten up when quiet,
which generally improves the risk/reward profile over fixed offsets.
"""

class Strategy:
    def __init__(self, interval="5m", size=1, stop_mult=1.0, target_mult=1.5, min_offset=0.5):
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
        stop = max(self.min_offset, rng * self.stop_mult)
        target = max(self.min_offset, rng * self.target_mult)

        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="mom-long"
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="mom-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
