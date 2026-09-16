import math

class Strategy:
    """Mean reversion using Bollinger Bands.
    
    Buys when the price crosses below the lower band, sells when it crosses above.
    The protective stop and target are scaled by the recent volatility (std dev).
    """
    def __init__(self, interval="5m", window=20, num_std=2.0, size=1, stop_std=2.0, target_std=2.0):
        self.interval = interval
        self.window = window
        self.num_std = num_std
        self.size = size
        self.stop_std = stop_std
        self.target_std = target_std

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
        std = math.sqrt(variance)
        
        if std == 0:
            return
            
        upper_band = mean + self.num_std * std
        lower_band = mean - self.num_std * std
        
        last_close = closes[-1]
        
        stop_dist = self.stop_std * std
        target_dist = self.target_std * std
        
        if last_close > upper_band:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="bb_short"
            )
        elif last_close < lower_band:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="bb_long"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
