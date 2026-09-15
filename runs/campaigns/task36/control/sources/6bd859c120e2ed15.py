"""Moving Average Crossover Strategy.

A standard trend-following strategy using fast and slow moving averages.
It enters on a crossover and uses a bracket order for exit.
"""

class Strategy:
    def __init__(self, interval="5m", fast_period=10, slow_period=30, size=1,
                 stop_offset=1.5, target_offset=3.0):
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

        bars = history.last(self.slow_period + 1)
        
        # Calculate fast MA
        fast_closes_curr = [b.close for b in bars[-self.fast_period:]]
        fast_ma_curr = sum(fast_closes_curr) / self.fast_period
        
        fast_closes_prev = [b.close for b in bars[-self.fast_period-1:-1]]
        fast_ma_prev = sum(fast_closes_prev) / self.fast_period
        
        # Calculate slow MA
        slow_closes_curr = [b.close for b in bars[-self.slow_period:]]
        slow_ma_curr = sum(slow_closes_curr) / self.slow_period
        
        slow_closes_prev = [b.close for b in bars[-self.slow_period-1:-1]]
        slow_ma_prev = sum(slow_closes_prev) / self.slow_period
        
        # Crossover logic
        crossover_up = fast_ma_prev <= slow_ma_prev and fast_ma_curr > slow_ma_curr
        crossover_down = fast_ma_prev >= slow_ma_prev and fast_ma_curr < slow_ma_curr
        
        if crossover_up:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="ma-long"
            )
        elif crossover_down:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="ma-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
