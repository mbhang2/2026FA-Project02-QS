"""Bollinger Bands mean reversion strategy.

Calculates a simple moving average and standard deviation over a rolling window
of closed bars. Enters long when the price closes below the lower band, and
short when it closes above the upper band. Exits are managed by a fixed bracket.
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
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.window:
            return
            
        window_bars = history.last(self.window)
        closes = [b.close for b in window_bars]
        
        sma = sum(closes) / self.window
        variance = sum((c - sma) ** 2 for c in closes) / self.window
        std = math.sqrt(variance)
        
        upper_band = sma + self.num_std * std
        lower_band = sma - self.num_std * std
        
        last_close = closes[-1]
        
        if last_close < lower_band:
            oid = ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-buy"
            )
            if oid is None:
                return
        elif last_close > upper_band:
            oid = ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-sell"
            )
            if oid is None:
                return

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
