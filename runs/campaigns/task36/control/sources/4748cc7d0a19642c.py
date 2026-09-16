"""
Reasoning:
The dataset is a simulated random walk, but the prompt hints at a planted momentum
signal by describing the current placeholder as "the dumbest strategy that can express
the planted signal" (buying if the last interval closed up). To capture this momentum
more robustly and avoid whipsaws, we implement a Donchian Channel Breakout strategy.
It buys when the close exceeds the highest high of the previous `lookback` bars,
and sells when it drops below the lowest low. The bracket offsets are scaled by the
channel's range, ensuring the risk parameters adapt to the local volatility.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=10, size=1, stop_mult=1.0, target_mult=2.0):
        self.interval = interval
        self.lookback = lookback
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        last_bar = window[-1]
        prior_bars = window[:-1]
        
        highest = max(b.high for b in prior_bars)
        lowest = min(b.low for b in prior_bars)
        rng = highest - lowest
        
        if rng <= 0:
            return

        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        if last_bar.close > highest:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="breakout-long"
            )
        elif last_bar.close < lowest:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="breakout-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
