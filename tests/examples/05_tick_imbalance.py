"""Signed-volume imbalance from raw ticks, decided on bars.

Subscribing to ticks is the only way to build order-flow features, and it is
the most expensive thing a strategy can do: every trade becomes a callback, the
event-driven walk is disabled because every tick must be delivered, and a tick
subscriber runs roughly 5x slower than the same strategy on bars alone. That
cost is charged to the wall-clock budget on purpose -- it is a real cost and it
should be visible.

**The `side is None` branch is not defensive programming.** The feed's aggressor
flag has three values -- bid-initiated, ask-initiated, and *unknown* -- and the
adapter maps unknown to `None`. It is rare (roughly one tick in 300,000 on the
NQ data) and that is exactly why folding it into one side never shows up in a
test and biases every order-flow number you compute. Decide what unknown means
and say so.
"""

from collections import deque

from src.core.types import Side


class Strategy:
    def __init__(self, interval="5m", window=200, threshold=0.25, size=1,
                 stop_offset=1.0, target_offset=1.5):
        self.interval = interval
        self.window = window
        self.threshold = threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.signed = deque(maxlen=window)
        self.unknown_side_ticks = 0

    def setup(self, ctx):
        ctx.subscribe(self.interval)
        ctx.subscribe("tick")

    def on_tick(self, ctx):
        t = ctx.tick
        if t.side is None:
            # Unknown aggressor. This strategy contributes nothing rather than
            # guessing -- and counts it, so the choice is visible afterwards
            # instead of being invisible in an aggregate.
            self.unknown_side_ticks += 1
            self.signed.append(0.0)
        elif t.side is Side.BUY:
            self.signed.append(float(t.size))
        else:
            self.signed.append(-float(t.size))

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        if len(self.signed) < self.window:
            return

        total = sum(abs(v) for v in self.signed)
        if total <= 0:
            return
        imbalance = sum(self.signed) / total

        if imbalance > self.threshold:
            ctx.buy(size=self.size, stop_offset=self.stop_offset,
                    target_offset=self.target_offset, tag="imbalance-long")
        elif imbalance < -self.threshold:
            ctx.sell(size=self.size, stop_offset=self.stop_offset,
                     target_offset=self.target_offset, tag="imbalance-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
