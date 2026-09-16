"""Bollinger Bands mean reversion strategy.

Subscribes to 5m bars. Computes the moving average and standard deviation
of the closing prices over a lookback window. Buys when the price crosses
below the lower band and sells when it crosses above the upper band.
Uses a bracket order for exits with offsets scaled by the recent volatility.
"""

import math

class Strategy:
    def __init__(self, interval="5m", lookback=20, num_std=2.0, size=1,
                 stop_mult=1.0, target_mult=1.0):
        self.interval = interval
        self.lookback = lookback
        self.num_std = num_std
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

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
            
        upper_band = mean + self.num_std * std
        lower_band = mean - self.num_std * std
        
        last = window[-1]
        
        stop_offset = std * self.stop_mult
        target_offset = std * self.target_mult
        
        # Ensure offsets are strictly positive to avoid ContextError
        if stop_offset <= 0 or target_offset <= 0:
            return
        
        if last.close < lower_band:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="bb-long"
            )
        elif last.close > upper_band:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="bb-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
