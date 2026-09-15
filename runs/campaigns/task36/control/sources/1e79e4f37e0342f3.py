"""
Moving Average Crossover Strategy.

Buys when the fast moving average crosses above the slow moving average.
Sells when the fast moving average crosses below the slow moving average.
Uses a bracket order with offsets for protection and profit taking.
"""

class Strategy:
    def __init__(self, interval="5m", fast_period=10, slow_period=30, size=1,
                 stop_offset=0.75, target_offset=0.75):
        self.interval = interval
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.slow_period + 1:
            return
            
        window = history.last(self.slow_period + 1)
        
        # Current moving averages
        fast_ma_0 = sum(b.close for b in window[-self.fast_period:]) / self.fast_period
        slow_ma_0 = sum(b.close for b in window[-self.slow_period:]) / self.slow_period
        
        # Previous moving averages
        fast_ma_1 = sum(b.close for b in window[-(self.fast_period + 1):-1]) / self.fast_period
        slow_ma_1 = sum(b.close for b in window[-(self.slow_period + 1):-1]) / self.slow_period
        
        # Crossover logic
        if fast_ma_1 <= slow_ma_1 and fast_ma_0 > slow_ma_0:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="ma-cross-long"
            )
        elif fast_ma_1 >= slow_ma_1 and fast_ma_0 < slow_ma_0:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="ma-cross-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
