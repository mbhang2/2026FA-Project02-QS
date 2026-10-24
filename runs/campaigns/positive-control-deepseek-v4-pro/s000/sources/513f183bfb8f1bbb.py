"""Enter at the earliest possible instant and hold.

Prior trials show the best score comes from a single market entry and no exit:
one fill, continuous exposure, no churn.  Previous entries waited for the first
bar close (or for a patient limit to fill).  This tries to improve on that by
using the session_open lifecycle event, which fires before the first bar close,
so we can submit the same one-entry order earlier and increase exposure without
adding any more fills or actions.

The order is a GTC market buy, so it stays alive across session boundaries and
the position is held to the end of the run.  Bracket exits are intentionally
avoided: every exit is an extra fill and prior trials show turnover hurts.
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
        """Queue one market entry if there is nothing working and no position."""
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return
        ctx.buy(size=self.size, tif="GTC", tag="initial-hold")

    def on_bars(self, ctx) -> None:
        # Retry on bars as a fallback if the session_open attempt did not exist.
        self._enter(ctx)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_open":
            self._enter(ctx)
