"""MACD Strategy.

Maintains Fast EMA, Slow EMA, and Signal EMA incrementally to avoid recomputing
over the entire history window. Enters on MACD histogram crossovers with a
bracket order.
"""

class Strategy:
    def __init__(self, interval="5m", fast_period=12, slow_period=26, signal_period=9,
                 size=1, stop_offset=1.5, target_offset=3.0):
        self.interval = interval
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        self.fast_ema = None
        self.slow_ema = None
        self.signal_ema = None
        self.prev_macd_hist = None

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if self.interval not in ctx.bars:
            return
            
        bar = ctx.bars[self.interval]
        
        # Initialize EMAs on the first bar
        if self.fast_ema is None:
            self.fast_ema = bar.close
            self.slow_ema = bar.close
            self.signal_ema = 0.0
            self.prev_macd_hist = 0.0
            return
            
        # Update EMAs incrementally
        fast_alpha = 2.0 / (self.fast_period + 1)
        slow_alpha = 2.0 / (self.slow_period + 1)
        signal_alpha = 2.0 / (self.signal_period + 1)
        
        self.fast_ema += fast_alpha * (bar.close - self.fast_ema)
        self.slow_ema += slow_alpha * (bar.close - self.slow_ema)
        
        macd_line = self.fast_ema - self.slow_ema
        self.signal_ema += signal_alpha * (macd_line - self.signal_ema)
        macd_hist = macd_line - self.signal_ema
        
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            self.prev_macd_hist = macd_hist
            return
            
        order_id = None
        if self.prev_macd_hist <= 0 and macd_hist > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="macd-long"
            )
        elif self.prev_macd_hist >= 0 and macd_hist < 0:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="macd-short"
            )
            
        self.prev_macd_hist = macd_hist
        
        if order_id is None:
            pass

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
