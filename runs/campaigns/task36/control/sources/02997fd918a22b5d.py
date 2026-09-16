"""
Moving Average Crossover Strategy.

Uses a fast and slow simple moving average to determine trend direction.
Enters on a crossover and uses a fixed bracket for exits.
"""

class Strategy:
    def __init__(self, interval="5m", fast_len=5, slow_len=15, size=1, 
                 stop_offset=0.75, target_offset=1.5):
        self.interval = interval
        self.fast_len = fast_len
        self.slow_len = slow_len
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.slow_len + 1:
            return

        # Take only the window we need to compute the previous and current MAs
        window = history.last(self.slow_len + 1)
        fast_window = window[-(self.fast_len + 1):]
        
        fast_ma_prev = sum(b.close for b in fast_window[:-1]) / self.fast_len
        fast_ma_curr = sum(b.close for b in fast_window[1:]) / self.fast_len
        
        slow_ma_prev = sum(b.close for b in window[:-1]) / self.slow_len
        slow_ma_curr = sum(b.close for b in window[1:]) / self.slow_len
        
        cross_up = fast_ma_prev <= slow_ma_prev and fast_ma_curr > slow_ma_curr
        cross_down = fast_ma_prev >= slow_ma_prev and fast_ma_curr < slow_ma_curr
        
        if cross_up:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="crossover-long"
            )
        elif cross_down:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="crossover-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
