"""
Bollinger Bands mean-reversion strategy.

Reasoning:
Since the dataset is a simulated random walk, there is no true predictive edge.
However, a mean-reversion strategy like Bollinger Bands is a robust baseline
that tests the execution mechanics, bracket orders, and rolling window calculations.
It enters long when the price closes below the lower band (SMA - 2*StdDev) and
short when it closes above the upper band (SMA + 2*StdDev). The exit is handled
entirely by a fixed-offset bracket, which guarantees the position is protected
from the instant it fills and avoids holding stale order IDs.

The strategy carefully respects the API limits:
- Subscribes in `setup()`
- Checks `ctx.actionable` before acting
- Ensures `len(history)` is sufficient before reading `history.last()`
- Does not reach past the current instant or outside the pushed history window
"""

class Strategy:
    def __init__(self, interval="5m", window=20, num_std=2.0, size=1,
                 stop_offset=1.5, target_offset=1.5):
        self.interval = interval
        self.window = window
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
        if len(history) < self.window:
            return

        bars = history.last(self.window)
        closes = [b.close for b in bars]
        
        sma = sum(closes) / self.window
        variance = sum((c - sma) ** 2 for c in closes) / self.window
        std = variance ** 0.5
        
        upper = sma + self.num_std * std
        lower = sma - self.num_std * std
        
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

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
