"""Bollinger Bands mean-reversion strategy.

Calculates a simple moving average and standard deviation over a lookback window.
Buys when the last closed bar is below the lower band, and sells when it is above
the upper band. Exits are handled entirely by fixed-offset brackets.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=20, std_dev=2.0, size=1,
                 stop_offset=1.5, target_offset=1.5):
        self.interval = interval
        self.lookback = lookback
        self.std_dev = std_dev
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback:
            return

        window = history.last(self.lookback)
        closes = [b.close for b in window]
        
        mean = sum(closes) / self.lookback
        variance = sum((c - mean) ** 2 for c in closes) / self.lookback
        std = variance ** 0.5

        if std <= 0:
            return

        upper = mean + self.std_dev * std
        lower = mean - self.std_dev * std
        last_close = window[-1].close

        if last_close < lower:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-long"
            )
        elif last_close > upper:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
