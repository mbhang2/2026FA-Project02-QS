"""Breakout of the last N closed bars, with the bracket given as distances.

Shows the two things most feature code needs: reading history without reaching
past the current instant, and sizing a bracket off a measured range rather than
a constant.
"""


class Strategy:
    def __init__(self, interval="5m", lookback=6, size=1, stop_mult=1.0,
                 target_mult=1.5):
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
        # `history` holds only closed bars. `history[-1]` is the one that just
        # closed; `history[len(history)]` raises LookaheadError rather than
        # returning something plausible. Short history is legitimate during
        # warmup, so check the length instead of catching.
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        last, prior = window[-1], window[:-1]
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if last.close > high:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="breakout-long")
        elif last.close < low:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="breakout-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
