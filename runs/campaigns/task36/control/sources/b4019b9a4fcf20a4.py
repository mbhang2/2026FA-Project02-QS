"""Mean Reversion Strategy using Bollinger Bands.

This strategy waits for the price to close outside the Bollinger Bands,
then enters a position in the opposite direction (mean reversion),
using a fixed bracket for exits.
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
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.window:
            return

        window_bars = history.last(self.window)
        closes = [b.close for b in window_bars]
        
        mean = sum(closes) / self.window
        variance = sum((c - mean) ** 2 for c in closes) / (self.window - 1) if self.window > 1 else 0
        std_dev = math.sqrt(variance)
        
        if std_dev == 0:
            return

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
