"""Momentum strategy with optimized risk-reward bracket.

Assumes the planted signal is a 1-bar momentum (AR(1) process) as captured by the baseline.
Uses an asymmetric bracket (target > stop) to maximize expectancy from the signal,
letting winners run further than the default 1:1 ratio.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        stop: float = 1.0,
        target: float = 2.0,
        size: int = 1,
    ):
        self.interval = interval
        self.stop = stop
        self.target = target
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]
        if last.close == last.open:
            return

        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop,
                target_offset=self.target,
                tag="long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop,
                target_offset=self.target,
                tag="short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
