"""Bollinger Bands mean reversion strategy.

Calculates a rolling mean and standard deviation over the last N closed bars.
When the price moves outside the upper or lower band, we assume the move is
overextended and take a mean-reverting position (sell if above upper band,
buy if below lower band). A bracket is armed immediately upon fill to manage
risk, avoiding unprotected exposure.
"""

class Strategy:
    def __init__(self, interval="5m", window=20, num_std=2.0, size=1,
                 stop_offset=1.5, target_offset=1.5):
        self.interval = interval
        self.window = window
        self.num_std = num_std
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.window:
            return
            
        window_bars = history.last(self.window)
        closes = [b.close for b in window_bars]
        
        mean = sum(closes) / self.window
        variance = sum((c - mean) ** 2 for c in closes) / self.window
        std = variance ** 0.5
        
        upper_band = mean + self.num_std * std
        lower_band = mean - self.num_std * std
        
        last = window_bars[-1]
        
        if last.close > upper_band:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-short"
            )
        elif last.close < lower_band:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-long"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
