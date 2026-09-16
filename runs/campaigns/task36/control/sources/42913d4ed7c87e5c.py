"""Mean reversion strategy using Bollinger Bands.

This strategy enters a position when the price crosses outside the Bollinger Bands,
expecting a reversion to the mean. It uses simple standard deviation and a bracket
order for risk management.
"""

import math

class Strategy:
    def __init__(
        self, 
        interval: str = "5m", 
        lookback: int = 20, 
        num_std: float = 2.0, 
        size: int = 1, 
        stop_offset: float = 2.0, 
        target_offset: float = 2.0
    ):
        self.interval = interval
        self.lookback = lookback
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
        if len(history) < self.lookback:
            return

        # Fetch only the required window to avoid recomputing on the entire history
        window = history.last(self.lookback)
        closes = [b.close for b in window]
        
        sma = sum(closes) / len(closes)
        variance = sum((c - sma) ** 2 for c in closes) / len(closes)
        std_dev = math.sqrt(variance)
        
        if std_dev == 0:
            return
            
        last = window[-1]
        
        # Mean reversion entry logic
        if last.close < sma - self.num_std * std_dev:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="bb-long"
            )
        elif last.close > sma + self.num_std * std_dev:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="bb-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
