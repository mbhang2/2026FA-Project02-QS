"""Enter long at the earliest possible instant and hold to the end.

The best prior result came from one GTC market entry, no exit, continuous
exposure. Previous attempts with churn, brackets, or low exposure scored worse,
so this keeps the same shape: submit a single long entry at the first
session_open, retry on bars as a fallback, and never exit.
"""

from __future__ import annotations


class Strategy:
    """Single GTC market entry at the first actionable instant, hold to end."""

    def __init__(self, *, interval: str = "5m", size: int = 1):
        self.interval = interval
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _enter(self, ctx) -> None:
        """Queue one long entry if there is nothing working and no position."""
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return
        ctx.buy(size=self.size, tif="GTC", tag="initial-hold")

    def on_bars(self, ctx) -> None:
        # Fallback in case the session_open entry attempt was not actionable or
        # otherwise did not happen.  Once flat and no order is working, enter.
        self._enter(ctx)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_open":
            self._enter(ctx)
