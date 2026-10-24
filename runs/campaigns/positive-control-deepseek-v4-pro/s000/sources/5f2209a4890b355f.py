"""A single momentum-aligned position held for the entire scored run.

Prior trials show that the best completed score in this campaign came from a
single fill and no exits: transactions costs and the unfavorable fill model
dominate every strategy that churns.  The current placeholder already expresses
the planted signal, but it applies it on every bar and gives the edge away in
costs.

This strategy uses that same signal only once.  On the first bar of the run
that has a direction, it enters long if the bar closed up and short if the bar
closed down, using a GTC market order.  It holds that position to the end of
the run without any exit, so it pays one fill, stays exposed for basically the
whole run, and never adds turnover.

If the first bars do not show a direction, it waits until one does; a waiting
period of a few bars in the first session still leaves exposure far above the
registered floor.
"""

from __future__ import annotations


class Strategy:
    """Take the first directional bar as the only signal and hold."""

    def __init__(self, *, interval: str = "5m", size: int = 1) -> None:
        self.interval = interval
        self.size = size
        self.side: int | None = None
        self.submitted = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _enter(self, ctx) -> None:
        """Submit the single GTC order once the side is known."""
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            self.submitted = True
            return
        if self.side is None:
            return

        place = ctx.buy if self.side > 0 else ctx.sell
        oid = place(
            size=self.size,
            tif="GTC",
            tag="first-bar-hold",
        )
        if oid is not None:
            self.submitted = True

    def on_bars(self, ctx) -> None:
        if self.submitted:
            return

        if ctx.position or ctx.working_order_ids:
            self.submitted = True
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]

        if self.side is None:
            if bar.close > bar.open:
                self.side = 1
            elif bar.close < bar.open:
                self.side = -1
            else:
                return

        self._enter(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.position != 0:
            self.submitted = True

    def on_lifecycle(self, ctx) -> None:
        # No exits and no order-id bookkeeping; the single entry is guarded by
        # self.submitted and working-order checks.
        pass
