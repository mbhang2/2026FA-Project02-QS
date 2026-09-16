"""Bollinger Bands mean reversion strategy.

Buys when the price drops below the lower Bollinger Band and sells when it
rises above the upper band. Uses a fixed bracket for exits.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=10, std_dev=1.5, size=1, 
                 stop_offset=1.0, target_offset=1.0):
        self.interval = interval
        self.lookback = lookback
        self.std_dev = std_dev
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.lookback:
            return
            
        window = history.last(self.lookback)
        closes = [b.close for b in window]
        
        sma = sum(closes) / self.lookback
        variance = sum((c - sma) ** 2 for c in closes) / self.lookback
        std = variance ** 0.5
        
        upper_band = sma + self.std_dev * std
        lower_band = sma - self.std_dev * std
        
        last_close = closes[-1]
        
        if last_close < lower_band:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb_buy"
            )
        elif last_close > upper_band:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb_sell"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
