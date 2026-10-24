"""Long-only buy-and-hold from the first actionable instant.

Prior trials in this campaign show that the best score comes from a single
continuous long position: one fill, no exits, maximum exposure.  The earlier
attempt that placed a GTC market order on the first session_open was the best
so far because it enters at the earliest possible instant and never churns.

This version keeps that same shape, with two small robustness changes:
- it subscribes to 1-minute bars so the bar-callback fallback fires earlier if
  the session_open attempt somehow did not result in a working order, and
- it also retries on an order_rejected lifecycle event, so a transient
  rejection cannot silently leave the strategy flat.

No stop or target is used: every exit is an extra fill and prior trials show
turnover hurts on this simulated dataset.
"""

from __future__ import annotations


class Strategy:
    """One GTC market buy at the first actionable instant, held to the end."""

    def __init__(self, *, interval: str = "1m", size: int = 1):
        self.interval = interval
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _enter(self, ctx) -> None:
        """Queue one market entry if there is no position and nothing working."""
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return
        ctx.buy(size=self.size, tif="GTC", tag="buy-and-hold")

    def on_bars(self, ctx) -> None:
        # Fallback entry point if the session_open attempt did not stick.
        self._enter(ctx)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_open":
            self._enter(ctx)
        elif ctx.event.kind == "order_rejected":
            # Retry once a rejected order clears from working_order_ids.
            self._enter(ctx)
