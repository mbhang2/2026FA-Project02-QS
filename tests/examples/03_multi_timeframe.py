"""Five-minute entries, filtered by the previous session's daily bar.

Two subscriptions. When both close at the same instant they arrive in **one**
callback, so "did my fast bar close?" is a real question -- and `ctx.bars` is
the answer: it holds exactly the bars that closed in this callback, addressed by
whatever you subscribed with.

`ctx.history(...)` is the other half: the closed bars for a subscription
whenever they closed, which is what a decision reads.
"""


class Strategy:
    def __init__(self, fast="5m", slow="1d", lookback=6, size=1,
                 stop_offset=1.0, target_offset=1.5):
        self.fast = fast
        self.slow = slow
        self.lookback = lookback
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx):
        ctx.subscribe(self.fast)
        # A daily subscription on this data closes once per session -- a session
        # is shorter than a day, and any bar still open at the session close is
        # flushed there.
        ctx.subscribe(self.slow)

    def on_bars(self, ctx):
        # Act only when the fast bar closed. The slow one closing is a session
        # boundary, not a decision point.
        if self.fast not in ctx.bars:
            return
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        daily = ctx.history(self.slow)
        if not len(daily):
            return                      # no completed session yet
        prev = daily[-1]
        bias = prev.close - prev.open
        if bias == 0:
            return

        fast = ctx.history(self.fast)
        if len(fast) < self.lookback:
            return
        window = fast.last(self.lookback)
        rising = window[-1].close > window[0].close

        # Take only the fast signal that agrees with the last completed session.
        if bias > 0 and rising:
            ctx.buy(size=self.size, stop_offset=self.stop_offset,
                    target_offset=self.target_offset, tag="with-trend-long")
        elif bias < 0 and not rising:
            ctx.sell(size=self.size, stop_offset=self.stop_offset,
                     target_offset=self.target_offset, tag="with-trend-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
