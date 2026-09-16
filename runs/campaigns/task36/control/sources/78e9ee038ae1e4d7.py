"""Bollinger Band Mean Reversion strategy.

Calculates a simple moving average and standard deviation over a lookback window.
Enters long when the price drops below the lower band, and short when it exceeds 
the upper band. Exits are handled by a bracket order with fixed offsets.
"""

import math

class Strategy:
    def __init__(self, interval="5m", lookback=20, std_devs=2.0, size=1,
                 stop_offset=1.5, target_offset=1.5):
        self.interval = interval
        self.lookback = lookback
        self.std_devs = std_devs
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
        std_dev = math.sqrt(variance)
        
        if std_dev == 0:
            return
            
        last_close = closes[-1]
        upper_band = mean + self.std_devs * std_dev
        lower_band = mean - self.std_devs * std_dev
        
        if last_close > upper_band:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="bb-short"
            )
        elif last_close < lower_band:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="bb-long"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
