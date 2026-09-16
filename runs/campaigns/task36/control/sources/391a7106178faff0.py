"""
Bollinger Bands mean-reversion strategy.

Computes a simple moving average and standard deviation over a lookback window.
If the most recent close is above the upper band, it enters short.
If the most recent close is below the lower band, it enters long.
Exits are managed by the engine using brackets based on fixed stop and target offsets.
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
            
        upper = mean + self.num_std * std
        lower = mean - self.num_std * std
        
        last_close = closes[-1]
        
        if last_close > upper:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-short"
            )
        elif last_close < lower:
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
