"""Mean reversion strategy using Bollinger Bands on 5m bars.

Calculates the rolling mean and standard deviation over a 20-bar window.
Enters long when the close crosses below the lower band, and short when it
crosses above the upper band. Uses a fixed bracket for exits.
"""

import math


class Strategy:
    def __init__(self, interval="5m", window=20, num_std=2.0, size=1,
                 stop_offset=2.0, target_offset=2.0):
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
        std_dev = math.sqrt(variance)

        upper_band = mean + self.num_std * std_dev
        lower_band = mean - self.num_std * std_dev

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
