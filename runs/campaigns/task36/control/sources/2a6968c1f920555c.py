"""
Bollinger Band mean reversion strategy.
Buys when the price drops below the lower band and sells when it exceeds the upper band.
Uses a bracket order to manage risk and take profit, minimizing time in the market.
"""

import math

class Strategy:
    def __init__(self, interval="5m", lookback=20, std_dev=2.5, size=1, stop_offset=4.0, target_offset=4.0):
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
        
        ma = sum(closes) / self.lookback
        variance = sum((c - ma) ** 2 for c in closes) / self.lookback
        std = math.sqrt(variance)
        
        if std == 0:
            return
            
        upper_band = ma + self.std_dev * std
        lower_band = ma - self.std_dev * std
        
        last_close = closes[-1]
        
        if last_close < lower_band:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="bb_long"
            )
        elif last_close > upper_band:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="bb_short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
