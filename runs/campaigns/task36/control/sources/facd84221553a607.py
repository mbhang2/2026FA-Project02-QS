"""
Dynamic Momentum Strategy.
Capitalizes on the planted momentum signal by using the previous bar's direction.
Uses dynamic bracket sizing based on a short lookback window's average range to adapt 
to the simulated random walk's volatility, improving risk-reward capture.
"""

class Strategy:
    def __init__(
        self, 
        interval: str = "5m", 
        lookback: int = 5, 
        stop_mult: float = 1.0, 
        target_mult: float = 2.0, 
        size: int = 1, 
        min_offset: float = 0.25
    ):
        self.interval = interval
        self.lookback = lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_offset = min_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.lookback:
            return
            
        window = history.last(self.lookback)
        last = window[-1]
        
        # Calculate average range over the lookback to scale brackets dynamically
        avg_range = sum(b.high - b.low for b in window) / self.lookback
        
        stop = max(avg_range * self.stop_mult, self.min_offset)
        target = max(avg_range * self.target_mult, self.min_offset)
        
        if last.close > last.open:
            ctx.buy(
                size=self.size, 
                stop_offset=stop, 
                target_offset=target, 
                tag="momentum-long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size, 
                stop_offset=stop, 
                target_offset=target, 
                tag="momentum-short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
