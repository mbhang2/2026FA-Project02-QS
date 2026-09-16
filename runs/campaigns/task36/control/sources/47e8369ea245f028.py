"""Dual Moving Average Crossover Strategy.

Buys when the fast moving average crosses above the slow moving average.
Sells when the fast moving average crosses below the slow moving average.
Uses bracket orders for exits to manage risk immediately upon fill.
"""

class Strategy:
    def __init__(self, interval: str = "5m", fast_period: int = 10, 
                 slow_period: int = 30, size: int = 1,
                 stop_offset: float = 1.0, target_offset: float = 1.0):
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
        current_fast_window = window[-self.fast_period:]
        current_slow_window = window[-self.slow_period:]
        
        current_fast_ma = sum(b.close for b in current_fast_window) / self.fast_period
        current_slow_ma = sum(b.close for b in current_slow_window) / self.slow_period
        
        # Previous moving averages (shifted by 1 bar)
        prev_fast_window = window[-(self.fast_period + 1):-1]
        prev_slow_window = window[-(self.slow_period + 1):-1]
        
        prev_fast_ma = sum(b.close for b in prev_fast_window) / self.fast_period
        prev_slow_ma = sum(b.close for b in prev_slow_window) / self.slow_period
        
        # Crossover logic
        bullish_cross = prev_fast_ma <= prev_slow_ma and current_fast_ma > current_slow_ma
        bearish_cross = prev_fast_ma >= prev_slow_ma and current_fast_ma < current_slow_ma
        
        if bullish_cross:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="ma-cross-long"
            )
        elif bearish_cross:
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
