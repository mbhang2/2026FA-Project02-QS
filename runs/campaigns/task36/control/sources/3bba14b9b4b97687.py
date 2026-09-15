"""Mean reversion strategy using Bollinger Bands.

Since the dataset is a simulated random walk, there are no long-term trends to
follow. This strategy attempts to fade short-term extremes by selling when the
price closes above the upper Bollinger Band and buying when it closes below the
lower band. The stop and target distances are scaled dynamically based on the
recent volatility (standard deviation) to adapt to the random walk's current
dispersion.
"""

import math


class Strategy:
    def __init__(self, interval: str = "5m", lookback: int = 20, num_std: float = 2.0,
                 size: int = 1, stop_mult: float = 1.5, target_mult: float = 1.0):
        self.interval = interval
        self.lookback = lookback
        self.num_std = num_std
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

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
        
        mean = sum(closes) / self.lookback
        variance = sum((c - mean) ** 2 for c in closes) / self.lookback
        std = math.sqrt(variance)
        
        if std <= 0.0:
            return
            
        upper = mean + self.num_std * std
        lower = mean - self.num_std * std
        
        last_close = closes[-1]
        
        stop_dist = self.stop_mult * std
        target_dist = self.target_mult * std
        
        if stop_dist <= 0 or target_dist <= 0:
            return
            
        if last_close > upper:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="bb-short"
            )
        elif last_close < lower:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="bb-long"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
