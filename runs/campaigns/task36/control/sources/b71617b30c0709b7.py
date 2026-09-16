"""
Bollinger Bands mean-reversion strategy.

This strategy computes a simple moving average and standard deviation over a rolling
window of closed bars. It enters long when the close price drops below the lower band,
and enters short when it rises above the upper band. The position is protected by a
bracket order (stop loss and take profit) defined by fixed offsets from the fill price.
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
        std_dev = variance ** 0.5
        
        if std_dev <= 0:
            return
            
        last_close = closes[-1]
        upper_band = mean + self.num_std * std_dev
        lower_band = mean - self.num_std * std_dev
        
        if last_close < lower_band:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb_long"
            )
        elif last_close > upper_band:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb_short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
