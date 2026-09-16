"""
EMA Crossover strategy.
Captures momentum using a fast and slow Exponential Moving Average.
"""

class Strategy:
    def __init__(self, interval="5m", fast=5, slow=15, size=1, stop_offset=1.0, target_offset=2.0):
        self.interval = interval
        self.fast = fast
        self.slow = slow
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.slow + 1:
            return
            
        window = history.last(self.slow + 1)
        closes = [b.close for b in window]
        
        fast_alpha = 2.0 / (self.fast + 1)
        slow_alpha = 2.0 / (self.slow + 1)
        
        fast_ema = closes[0]
        slow_ema = closes[0]
        
        for price in closes[1:-1]:
            fast_ema = (price - fast_ema) * fast_alpha + fast_ema
            slow_ema = (price - slow_ema) * slow_alpha + slow_ema
            
        fast_prev = fast_ema
        slow_prev = slow_ema
        
        fast_curr = (closes[-1] - fast_prev) * fast_alpha + fast_prev
        slow_curr = (closes[-1] - slow_prev) * slow_alpha + slow_prev
        
        if fast_prev <= slow_prev and fast_curr > slow_curr:
            ctx.buy(size=self.size, stop_offset=self.stop_offset, target_offset=self.target_offset, tag="ema-long")
        elif fast_prev >= slow_prev and fast_curr < slow_curr:
            ctx.sell(size=self.size, stop_offset=self.stop_offset, target_offset=self.target_offset, tag="ema-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
