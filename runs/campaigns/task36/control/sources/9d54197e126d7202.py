"""
Mean Reversion strategy using Bollinger Bands.
Buys when the price drops below the lower band, sells when it rises above the upper band.
Uses a fixed stop loss and take profit bracket.
"""

import math

class Strategy:
    def __init__(self, interval="5m", lookback=20, std_dev=2.0, size=1,
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
        
        sma = sum(closes) / self.lookback
        variance = sum((c - sma) ** 2 for c in closes) / self.lookback
        std = math.sqrt(variance)
        
        if std <= 0:
            return
            
        last_close = closes[-1]
        
        if last_close < sma - self.std_dev * std:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-buy"
            )
        elif last_close > sma + self.std_dev * std:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-sell"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
