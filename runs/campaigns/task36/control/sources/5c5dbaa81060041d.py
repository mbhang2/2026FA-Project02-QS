"""Bollinger Bands mean reversion strategy.

Buys when the price drops below the lower band, sells when it rises above the
upper band. Uses a fixed bracket for exit.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=20, dev_mult=2.0, size=1,
                 stop_offset=1.5, target_offset=3.0):
        self.interval = interval
        self.lookback = lookback
        self.dev_mult = dev_mult
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
        
        last = window[-1]
        
        if std == 0:
            return
            
        upper = mean + self.dev_mult * std
        lower = mean - self.dev_mult * std
        
        if last.close < lower:
            ctx.buy(size=self.size, stop_offset=self.stop_offset,
                    target_offset=self.target_offset, tag="bb-long")
        elif last.close > upper:
            ctx.sell(size=self.size, stop_offset=self.stop_offset,
                     target_offset=self.target_offset, tag="bb-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
