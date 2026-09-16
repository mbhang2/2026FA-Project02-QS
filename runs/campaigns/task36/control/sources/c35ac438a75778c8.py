"""
A momentum strategy that exploits the planted 1-bar momentum signal,
using an average range measure to dynamically size the bracket orders.
This adapts the stop and target offsets to the current volatility of the walk
rather than using fixed constants.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=14, size=1, stop_mult=1.0, target_mult=1.5):
        self.interval = interval
        self.lookback = lookback
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
        
        # Simple volatility measure (average bar range)
        ranges = [b.high - b.low for b in window]
        avg_range = sum(ranges) / len(ranges)
        
        if avg_range <= 0:
            return
            
        last = window[-1]
        
        stop = avg_range * self.stop_mult
        target = avg_range * self.target_mult
        
        if last.close > last.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target, tag="mom-long")
        elif last.close < last.open:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target, tag="mom-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
