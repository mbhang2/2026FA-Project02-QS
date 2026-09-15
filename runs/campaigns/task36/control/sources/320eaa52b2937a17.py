"""Bollinger Bands mean reversion strategy.

Buys when the price crosses below the lower band, sells when it crosses above
the upper band. Exits are managed entirely by brackets armed at the instant of
the fill. Uses a simple moving average and standard deviation over a rolling
window of closed bars.
"""

import math


class Strategy:
    def __init__(self, interval="5m", window=20, num_std=2.0, size=1,
                 stop_offset=1.5, target_offset=1.5):
        self.interval = interval
        self.window = window
        self.num_std = num_std
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
        if len(history) < self.window:
            return

        window_bars = history.last(self.window)
        closes = [b.close for b in window_bars]
        
        mean = sum(closes) / self.window
        variance = sum((c - mean) ** 2 for c in closes) / self.window
        std = math.sqrt(variance)
        
        if std == 0:
            return
            
        upper_band = mean + self.num_std * std
        lower_band = mean - self.num_std * std
        
        last = window_bars[-1]
        
        if last.close < lower_band:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-long"
            )
        elif last.close > upper_band:
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
