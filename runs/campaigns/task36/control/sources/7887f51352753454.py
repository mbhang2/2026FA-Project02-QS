"""Bollinger Bands mean-reversion strategy.

Calculates a simple moving average and standard deviation over a lookback window.
Enters long when the price closes below the lower band, and short when it closes
above the upper band. Exits are handled by a fixed bracket.
"""

import math

class Strategy:
    def __init__(self, interval="5m", lookback=20, std_dev=2.5, size=1,
                 stop_offset=2.0, target_offset=2.0):
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
        std = math.sqrt(variance)
        
        if std == 0:
            return
            
        last_close = closes[-1]
        upper_band = mean + self.std_dev * std
        lower_band = mean - self.std_dev * std
        
        if last_close < lower_band:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-long"
            )
        elif last_close > upper_band:
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
