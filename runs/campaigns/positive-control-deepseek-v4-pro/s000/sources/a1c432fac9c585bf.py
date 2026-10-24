"""Short hold with a patient limit entry above the market.

The best prior trial was a one-sided long position entered early and held to
the end: score -0.0976.  A short position over the same realised path should
capture the opposite mark-to-market P&L.  This mirrors that strategy exactly,
but sells instead of buying.  A GTC sell limit is rested a little above the
first closed bar; if it has not filled by the next bar we cancel it and sell at
market, so the strategy is exposed almost immediately and held to the end.
"""

from __future__ import annotations


class Strategy:
    """Mirror of the best long-hold strategy, on the short side."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        entry_limit_offset: float = 0.5,
        max_wait_bars: int = 1,
    ):
        self.interval = interval
        self.size = size
        self.entry_limit_offset = entry_limit_offset
        self.max_wait_bars = max_wait_bars

        self._started = False
        self._bars_waiting = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position:
            return  # already short; hold to the end

        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]

        working_limit = ctx.find_orders(tag="short-entry-limit")
        if working_limit:
            self._bars_waiting += 1
            if self._bars_waiting >= self.max_wait_bars:
                ctx.cancel(working_limit[0].id)
                ctx.sell(
                    size=self.size,
                    tif="GTC",
                    tag="short-entry-market",
                )
            return

        if ctx.working_order_ids:
            return

        if not self._started:
            order_id = ctx.sell(
                size=self.size,
                type="limit",
                limit=last.close + self.entry_limit_offset,
                tif="GTC",
                tag="short-entry-limit",
            )
            if order_id is not None:
                self._started = True
                self._bars_waiting = 0
            return

        # No position, no working limit, no working order: the limit was
        # filled and exited?  Actually if it filled we would have a position.
        # This branch re-enters if an order was cancelled or rejected.
        ctx.sell(
            size=self.size,
            tif="GTC",
            tag="short-entry-market",
        )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
