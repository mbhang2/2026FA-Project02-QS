"""
Reasoning:
The previous placeholder strategy expressed a 1-bar momentum signal. To improve upon it, 
we can use a more robust momentum indicator: a Donchian channel breakout. By taking 
positions when the most recent close exceeds the highest high (or lowest low) of the 
previous N bars, we can capture sustained momentum. We size the protective stop and 
target brackets as multiples of the measured breakout range, adjusting to the volatility 
of the recent window rather than using fixed absolute offsets. This should yield better 
risk-adjusted returns if the planted signal involves trend or momentum.
"""

class Strategy:
    def __init__(
        self, 
        interval: str = "5m", 
        lookback: int = 6, 
        size: int = 1, 
        stop_mult: float = 1.0, 
        target_mult: float = 1.5
    ):
        self.interval = interval
        self.lookback = lookback
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        last = window[-1]
        prior = window[:-1]
        
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        
        if rng <= 0:
            return
            
        stop = rng * self.stop_mult
        target = rng * self.target_mult
        
        if last.close > high:
            ctx.buy(
                size=self.size, 
                stop_offset=stop, 
                target_offset=target, 
                tag="breakout-long"
            )
        elif last.close < low:
            ctx.sell(
                size=self.size, 
                stop_offset=stop, 
                target_offset=target, 
                tag="breakout-short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
