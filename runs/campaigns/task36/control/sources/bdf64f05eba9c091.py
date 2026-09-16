"""SMA Crossover Strategy.

A simple moving average crossover strategy. It waits for the fast SMA to cross
the slow SMA to determine the trend, and enters a position with a bracket order.
"""

class Strategy:
    def __init__(self, interval="5m", fast_period=9, slow_period=21, size=1,
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
        
        prev_fast_sma = sum(b.close for b in window[-(self.fast_period + 1):-1]) / self.fast_period
        prev_slow_sma = sum(b.close for b in window[-(self.slow_period + 1):-1]) / self.slow_period
        
        curr_fast_sma = sum(b.close for b in window[-self.fast_period:]) / self.fast_period
        curr_slow_sma = sum(b.close for b in window[-self.slow_period:]) / self.slow_period

        if prev_fast_sma <= prev_slow_sma and curr_fast_sma > curr_slow_sma:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="sma-cross-up"
            )
        elif prev_fast_sma >= prev_slow_sma and curr_fast_sma < curr_slow_sma:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="sma-cross-down"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
