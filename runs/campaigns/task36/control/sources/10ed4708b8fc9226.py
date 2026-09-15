"""Mean reversion strategy using Bollinger Bands.

This strategy calculates a simple moving average and standard deviation over
a rolling window of closed bars. It enters long when the price drops below
the lower band and short when it rises above the upper band, using fixed
offset brackets for protection and take-profit.
"""

class Strategy:
    def __init__(self, interval: str = "5m", lookback: int = 20, 
                 std_dev: float = 2.0, size: int = 1, 
                 stop_offset: float = 1.0, target_offset: float = 1.0):
        self.interval = interval
        self.lookback = lookback
        self.std_dev = std_dev
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.lookback:
            return
            
        window = history.last(self.lookback)
        closes = [b.close for b in window]
        sma = sum(closes) / self.lookback
        variance = sum((c - sma) ** 2 for c in closes) / self.lookback
        std = variance ** 0.5
        
        upper = sma + self.std_dev * std
        lower = sma - self.std_dev * std
        
        last_close = closes[-1]
        
        if last_close < lower:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="bb-long"
            )
        elif last_close > upper:
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
