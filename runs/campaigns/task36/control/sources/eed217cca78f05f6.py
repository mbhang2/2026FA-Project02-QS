"""Moving Average Crossover Strategy.

Calculates a fast and slow moving average and trades when they cross.
Uses brackets for risk management.
"""

class Strategy:
    def __init__(self, interval="5m", fast=10, slow=30, size=1, stop_offset=1.0, target_offset=1.5):
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

        # Get exactly what we need for the current and previous MAs
        window = history.last(self.slow + 1)
        closes = [b.close for b in window]

        fast_prev = sum(closes[-(self.fast + 1):-1]) / self.fast
        slow_prev = sum(closes[-(self.slow + 1):-1]) / self.slow

        fast_curr = sum(closes[-self.fast:]) / self.fast
        slow_curr = sum(closes[-self.slow:]) / self.slow

        if fast_prev <= slow_prev and fast_curr > slow_curr:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="ma-cross-up"
            )
        elif fast_prev >= slow_prev and fast_curr < slow_curr:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="ma-cross-down"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
