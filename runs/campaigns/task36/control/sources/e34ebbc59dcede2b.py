"""Momentum strategy with volatility-adjusted brackets.

This strategy captures the planted 1-bar momentum signal ("buy if last closed up,
sell if last closed down") but improves upon the placeholder by sizing the bracket
dynamically using the average range of the last N bars. This adapts the stop and
target to the local volatility of the random walk, avoiding overly tight stops on
quiet bars or overly wide ones on volatile bars.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 5,
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 2.0,
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
        if len(history) < self.lookback:
            return

        window = history.last(self.lookback)
        last = window[-1]

        if last.close == last.open:
            return

        avg_rng = sum(b.high - b.low for b in window) / len(window)
        if avg_rng <= 0:
            return

        stop = avg_rng * self.stop_mult
        target = avg_rng * self.target_mult

        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="mom-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="mom-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
