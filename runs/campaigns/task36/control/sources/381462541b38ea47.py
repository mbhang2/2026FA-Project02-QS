"""Mean reversion strategy using Bollinger Bands.

Calculates a rolling mean and standard deviation over the last N bars.
Buys when the close price drops below the lower band, and sells when it 
exceeds the upper band. Exits are managed entirely by the engine using 
bracket orders with fixed offsets.
"""

class Strategy:
    def __init__(
        self, 
        interval: str = "5m", 
        window: int = 20, 
        num_std: float = 2.0, 
        size: int = 1, 
        stop_offset: float = 2.0, 
        target_offset: float = 2.0
    ):
        self.interval = interval
        self.window = window
        self.num_std = num_std
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
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
        
        if std == 0:
            return
            
        upper_band = mean + self.num_std * std
        lower_band = mean - self.num_std * std
        
        last_close = closes[-1]
        
        if last_close > upper_band:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="bb-short"
            )
        elif last_close < lower_band:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="bb-long"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
