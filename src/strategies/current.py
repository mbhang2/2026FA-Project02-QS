"""The agent's file. Everything else in the repo is immutable to it.

Right now it holds the dumbest strategy that can express the planted signal: buy
the next interval if the last one closed up, sell if it closed down, with a
bracket so the position is protected from the instant it fills rather than from
the next callback. On pure-noise data it has no edge by construction, which is
what makes the null half of calibration meaningful.
"""

from __future__ import annotations

from src.core.types import (
    Action,
    BarEvent,
    BracketSpec,
    FillEvent,
    LifecycleEvent,
    NS_PER_SECOND,
    OrderType,
    Side,
    Submit,
    Subscription,
    TimeInForce,
)

BAR = Subscription(interval_ns=5 * 60 * NS_PER_SECOND)


class MomentumStrategy:
    def __init__(self, *, stop: float = 0.75, target: float = 0.75, size: int = 1):
        self.stop = stop
        self.target = target
        self.size = size

    def setup(self, ctx) -> list[Subscription]:
        return [BAR]

    def on_bars(self, ev: BarEvent, ctx) -> list[Action]:
        if ctx.position != 0 or ctx.working_order_ids:
            return []          # one position at a time; the bracket does the exits
        history = ctx.bars[BAR.sub_id]
        if len(history) < 1:
            return []
        last = history[-1]     # the bar that just closed; [len] would raise
        if last.close == last.open:
            return []
        side = Side.BUY if last.close > last.open else Side.SELL
        return [
            Submit(
                side=side,
                size=self.size,
                order_type=OrderType.MARKET,
                tif=TimeInForce.DAY,
                bracket=BracketSpec(stop_offset=self.stop, target_offset=self.target),
                tag="entry",
            )
        ]

    def on_fill(self, ev: FillEvent, ctx) -> list[Action]:
        return []

    def on_lifecycle(self, ev: LifecycleEvent, ctx) -> list[Action]:
        return []
