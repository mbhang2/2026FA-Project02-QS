"""Mean reversion strategy using Bollinger Bands.

Buys when the close is below the lower band, sells when above the upper band.
Uses a bracket order to manage the trade.
"""

class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        window: int = 20,
        num_std: float = 2.0,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 1.5,
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

        bars = history.last(self.window)
        closes = [b.close for b in bars]
        
        mean = sum(closes) / self.window
        variance = max(0.0, sum((c - mean) ** 2 for c in closes) / self.window)
        std_dev = variance ** 0.5
        
        upper_band = mean + self.num_std * std_dev
        lower_band = mean - self.num_std * std_dev
        
        last_close = closes[-1]
        
        if last_close < lower_band:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-long"
            )
        elif last_close > upper_band:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
