"""Bollinger Bands mean reversion strategy.

Subscribes to 5m bars. Calculates a 20-period moving average and standard deviation.
Goes long when price closes below the lower band, and short when it closes above the upper band.
Uses a bracket order with offsets for protection and profit taking.
"""

import math

class Strategy:
    def __init__(self, interval="5m", window=20, dev_mult=1.5, size=1,
                 stop_offset=1.0, target_offset=1.0):
        self.interval = interval
        self.window = window
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
        if len(history) < self.window:
            return

        window_bars = history.last(self.window)
        closes = [b.close for b in window_bars]
        
        mean = sum(closes) / self.window
        variance = sum((c - mean) ** 2 for c in closes) / self.window
        std_dev = math.sqrt(variance)
        
        if std_dev == 0:
            return
            
        upper_band = mean + self.dev_mult * std_dev
        lower_band = mean - self.dev_mult * std_dev
        
        last_close = closes[-1]
        
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
