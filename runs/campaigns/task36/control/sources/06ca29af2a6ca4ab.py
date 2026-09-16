"""
A breakout strategy that enters on a new high/low over a lookback window,
using a bracket order for risk management scaled by the recent range.
"""

class Strategy:
    def __init__(
        self, 
        interval: str = "5m", 
        lookback: int = 12, 
        size: int = 1, 
        stop_mult: float = 1.0, 
        target_mult: float = 2.0
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
        last_bar = window[-1]
        prior_bars = window[:-1]
        
        highest_high = max(b.high for b in prior_bars)
        lowest_low = min(b.low for b in prior_bars)
        rng = highest_high - lowest_low
        
        if rng <= 0:
            return
            
        stop_offset = rng * self.stop_mult
        target_offset = rng * self.target_mult
        
        if last_bar.close > highest_high:
            ctx.buy(
                size=self.size, 
                stop_offset=stop_offset, 
                target_offset=target_offset, 
                tag="breakout-long"
            )
        elif last_bar.close < lowest_low:
            ctx.sell(
                size=self.size, 
                stop_offset=stop_offset, 
                target_offset=target_offset, 
                tag="breakout-short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
