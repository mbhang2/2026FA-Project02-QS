"""Bollinger Bands mean reversion strategy.

Calculates a simple moving average and standard deviation over a lookback window.
Enters long when the price closes below the lower band, and short when it closes
above the upper band. Uses dynamic brackets based on the measured standard deviation.
"""

import math

class Strategy:
    def __init__(self, interval="5m", lookback=20, std_mult=2.0, size=1, stop_mult=1.0, target_mult=1.5):
        self.interval = interval
        self.lookback = lookback
        self.std_mult = std_mult
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

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
        
        mean = sum(closes) / self.lookback
        variance = sum((c - mean) ** 2 for c in closes) / self.lookback
        std = math.sqrt(variance)
        
        if std <= 0:
            return
            
        upper = mean + self.std_mult * std
        lower = mean - self.std_mult * std
        
        last_close = closes[-1]
        
        stop_dist = std * self.stop_mult
        target_dist = std * self.target_mult
        
        if stop_dist < 0.25 or target_dist < 0.25:
            return
        
        if last_close < lower:
            ctx.buy(size=self.size, stop_offset=stop_dist, target_offset=target_dist, tag="bb-long")
        elif last_close > upper:
            ctx.sell(size=self.size, stop_offset=stop_dist, target_offset=target_dist, tag="bb-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
