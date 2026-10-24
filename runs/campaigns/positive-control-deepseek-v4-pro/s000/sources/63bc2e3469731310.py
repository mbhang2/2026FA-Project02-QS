"""Minimal-turnover short-and-hold strategy.

The previous minimal-turnover long hold recorded a negative score. Flipping
the direction to a short at the same point avoids all of the turnover costs
that hurt the earlier trading strategies while maintaining continuous
exposure (so the exposure floor is met). With one GTC entry and no exit
until the run ends, the strategy only pays one commission and has no
lookahead or lifecycle risk.
"""

from __future__ import annotations


class Strategy:
    """Short once and hold to the end of the run."""

    def __init__(self, *, interval: str = "5m", size: int = 1):
        self.interval = interval
        self.size = size
        self._entered = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if self._entered or ctx.position or ctx.working_order_ids:
            return

        order_id = ctx.sell(
            size=self.size,
            tif="GTC",
            tag="hold-short",
        )
        if order_id is not None:
            self._entered = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
