"""
Momentum strategy with volatility-adjusted stops and targets.
Uses the baseline 1-bar momentum signal but improves risk management by sizing
the bracket dynamically based on recent bar ranges, aiming for a 2:1 reward-to-risk.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=12, stop_mult=1.0, target_mult=2.0, size=1):
        self.interval = interval
        self.lookback = lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.lookback:
            return
            
        window = history.last(self.lookback)
        last = window[-1]
        
        if last.close == last.open:
            return
            
        avg_range = sum(b.high - b.low for b in window) / self.lookback
        
        if avg_range <= 0:
            return
            
        stop = avg_range * self.stop_mult
        target = avg_range * self.target_mult
        
        if last.close > last.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target, tag="long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target, tag="short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
