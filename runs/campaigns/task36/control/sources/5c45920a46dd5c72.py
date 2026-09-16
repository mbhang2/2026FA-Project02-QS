"""
Volatility-adjusted momentum strategy.
Calculates a simple ATR over the last 14 bars, and uses it to set dynamic 
bracket offsets for a momentum entry.
"""

class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        atr_period: int = 14,
        atr_mult_stop: float = 1.0,
        atr_mult_target: float = 2.0,
        min_offset: float = 0.25,
        size: int = 1,
    ):
        self.interval = interval
        self.atr_period = atr_period
        self.atr_mult_stop = atr_mult_stop
        self.atr_mult_target = atr_mult_target
        self.min_offset = min_offset
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.atr_period + 1:
            return

        window = history.last(self.atr_period + 1)
        
        true_ranges = []
        for i in range(1, len(window)):
            curr = window[i]
            prev = window[i-1]
            tr = max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close)
            )
            true_ranges.append(tr)
            
        atr = sum(true_ranges) / self.atr_period
        
        stop_dist = max(self.min_offset, atr * self.atr_mult_stop)
        target_dist = max(self.min_offset, atr * self.atr_mult_target)
        
        last = window[-1]
        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
