"""Mean Reversion Strategy using Bollinger Bands.

Calculates a 20-period simple moving average and standard deviation on 5m bars.
Enters long when the price drops below the lower band, and short when it exceeds the upper band.
Exits are managed entirely by the engine using a symmetric bracket order.
"""

import math

class Strategy:
    def __init__(
        self, 
        interval: str = "5m", 
        lookback: int = 20, 
        num_std: float = 2.0, 
        size: int = 1, 
        stop_offset: float = 1.0, 
        target_offset: float = 1.0
    ):
        self.interval = interval
        self.lookback = lookback
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
        if len(history) < self.lookback:
            return
            
        window = history.last(self.lookback)
        closes = [b.close for b in window]
        
        sma = sum(closes) / self.lookback
        variance = max(0.0, sum((c - sma) ** 2 for c in closes) / self.lookback)
        std_dev = math.sqrt(variance)
        
        if std_dev == 0.0:
            return
            
        upper_band = sma + self.num_std * std_dev
        lower_band = sma - self.num_std * std_dev
        
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
