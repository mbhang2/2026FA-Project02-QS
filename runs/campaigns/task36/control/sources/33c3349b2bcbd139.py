"""Momentum strategy with EMA trend filter.

Builds on the baseline momentum signal by requiring the short-term momentum
(last bar's direction) to align with the medium-term trend (price vs EMA).
Uses an asymmetric bracket to capture extended moves.
"""

class Strategy:
    def __init__(self, interval="5m", ema_period=20, size=1, stop_offset=1.0, target_offset=2.0):
        self.interval = interval
        self.ema_period = ema_period
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        lookback = self.ema_period + 20
        if len(history) < lookback:
            return
            
        bars = history.last(lookback)
        closes = [b.close for b in bars]
        
        alpha = 2 / (self.ema_period + 1)
        ema_val = closes[0]
        for price in closes[1:]:
            ema_val = price * alpha + ema_val * (1 - alpha)
            
        last = bars[-1]
        
        if last.close == last.open:
            return
            
        is_up = last.close > last.open
        is_down = last.close < last.open
        
        above_ema = last.close > ema_val
        below_ema = last.close < ema_val
        
        if is_up and above_ema:
            ctx.buy(size=self.size, stop_offset=self.stop_offset, target_offset=self.target_offset, tag="mom-long")
        elif is_down and below_ema:
            ctx.sell(size=self.size, stop_offset=self.stop_offset, target_offset=self.target_offset, tag="mom-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
