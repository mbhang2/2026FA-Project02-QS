"""Bollinger Bands strategy.

Calculates a simple moving average and standard deviation over a lookback window.
Enters long when the price crosses below the lower band, and short when it crosses
above the upper band. Exits are managed entirely by brackets.
"""

class Strategy:
    def __init__(self, interval: str = "5m", lookback: int = 20, num_std: float = 2.0, 
                 size: int = 1, stop_offset: float = 0.75, target_offset: float = 0.75):
        self.interval = interval
        self.lookback = lookback
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
        if len(history) < self.lookback:
            return
            
        window = history.last(self.lookback)
        closes = [b.close for b in window]
        
        sma = sum(closes) / self.lookback
        variance = sum((c - sma) ** 2 for c in closes) / self.lookback
        std_dev = variance ** 0.5
        
        if std_dev == 0:
            return
            
        upper_band = sma + self.num_std * std_dev
        lower_band = sma - self.num_std * std_dev
        
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
