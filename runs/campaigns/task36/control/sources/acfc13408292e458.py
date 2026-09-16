"""Momentum strategy with a moving average trend filter.

Captures the planted momentum signal (following the last closed bar) but filters 
trades to only align with the recent trend, defined by a simple moving average. 
Uses asymmetric brackets (target > stop) to improve the risk/reward ratio.
"""

class Strategy:
    def __init__(self, interval="5m", ma_period=10, stop_offset=1.0, 
                 target_offset=2.5, size=1):
        self.interval = interval
        self.ma_period = ma_period
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.ma_period:
            return
            
        window = history.last(self.ma_period)
        last = window[-1]
        
        if last.close == last.open:
            return
            
        ma = sum(b.close for b in window) / self.ma_period
        
        if last.close > last.open and last.close > ma:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="long"
            )
        elif last.close < last.open and last.close < ma:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
