"""
Momentum strategy using Average Range for bracket sizing.

Exploits the planted momentum signal ("buy if the last bar closed up") but
improves upon the baseline by dynamically sizing the stop and target offsets
based on the recent volatility (Average Range over the last 14 bars).
This allows the strategy to capture larger moves during high volatility
while avoiding premature stop-outs.
"""

class Strategy:
    def __init__(self, interval="5m", atr_period=14, size=1, stop_mult=1.5, target_mult=3.0):
        self.interval = interval
        self.atr_period = atr_period
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.atr_period:
            return

        window = history.last(self.atr_period)
        avg_range = sum(b.high - b.low for b in window) / self.atr_period
        
        if avg_range <= 0:
            return

        # Ensure minimum offset to avoid immediate stop-outs on noise or rejection
        stop = max(avg_range * self.stop_mult, 0.25)
        target = max(avg_range * self.target_mult, 0.25)

        last = window[-1]
        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="mom-long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="mom-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
