"""Proposed strategy.

The baseline takes a 5m momentum signal with fixed 0.75 brackets. This variant:
- stays on the same 5m signal (last close vs open)
- requires signed order flow to agree (`bar.delta`), which avoids trades where
  the close moved against the flow
- scales the bracket to the bar's actual high-low range rather than a fixed
  price distance
- skips flat/zero-range bars
"""
from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", stop_mult: float = 0.9,
                 target_mult: float = 1.1, size: int = 1):
        self.interval = interval
        self.stop_mult = stop_mult
        self.target_mult = target_mult
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

        rng = last.high - last.low
        if rng <= 0:
            return

        delta = getattr(last, "delta", 0.0)

        stop = rng * self.stop_mult
        target = rng * self.target_mult
        if stop <= 0 or target <= 0:
            return

        if last.close > last.open and delta >= 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="entry",
            )
        elif last.close < last.open and delta <= 0:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="entry",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
