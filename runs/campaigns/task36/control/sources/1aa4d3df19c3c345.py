"""Mean Reversion Strategy.

Calculates a simple moving average over a short window. If the close price
deviates significantly from the MA, it takes a contrarian position, expecting
reversion. Exits are managed by brackets.
"""

class Strategy:
    def __init__(self, interval="5m", window=10, threshold=0.25, size=1,
                 stop_offset=0.75, target_offset=0.75):
        self.interval = interval
        self.window = window
        self.threshold = threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.window:
            return

        window_bars = history.last(self.window)
        ma = sum(b.close for b in window_bars) / self.window
        last_close = window_bars[-1].close

        diff = last_close - ma

        if diff < -self.threshold:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="mr-long"
            )
        elif diff > self.threshold:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="mr-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
