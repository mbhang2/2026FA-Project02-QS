"""Short-only buy-and-hold from the first actionable instant.

Prior trials: long-only buy-and-hold produced score -0.0976 with zero round
trips, and every strategy with more fills did worse. If the simulated price
process has a negative drift over the scored sessions, mirroring that exposure
with one GTC sell held to the end should have the opposite sign.

No stop or target is used: every exit is an extra fill, and prior trials show
turnover hurts on this dataset. The order is GTC so the short position survives
session closes and remains exposed throughout the scored sessions.
"""

from __future__ import annotations


class Strategy:
    """One GTC market sell at the first actionable instant, held to the end."""

    def __init__(self, *, interval: str = "1m", size: int = 1):
        self.interval = interval
        self.size = size
        self._entered = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _enter(self, ctx) -> None:
        if not ctx.actionable:
            return
        if self._entered or ctx.position or ctx.working_order_ids:
            return
        ctx.sell(size=self.size, tif="GTC", tag="short-hold")

    def on_bars(self, ctx) -> None:
        # Fallback entry point if the session_open attempt did not stick.
        self._enter(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            self._entered = True

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_open":
            self._enter(ctx)
        elif ctx.event.kind == "order_rejected":
            self._entered = False
            self._enter(ctx)
