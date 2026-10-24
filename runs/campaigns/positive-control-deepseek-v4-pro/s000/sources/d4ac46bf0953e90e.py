"""Long-only buy-and-hold with a patient one-tick limit entry.

Prior trials in this campaign show the best score comes from a single
continuous long position: one fill, no exits, maximum exposure.  The one
remaining edge is entry quality: a market order fills unfavourably, while a
resting GTC limit can buy a small down tick instead.

This version rests a GTC limit one tick below `last_price` at the first
actionable instant.  If the market trades down, the entry is cheaper than a
market fill.  If it has not filled by the close of the first 1-minute bar, the
strategy cancels the stale limit and buys at market, so exposure is not lost
waiting for a dip.  After that it holds to the end; every extra exit has hurt
prior trials.
"""

from __future__ import annotations


class Strategy:
    """One GTC long entry, patient limit first, market fallback, hold to end."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        size: int = 1,
        entry_offset: float = 0.25,
        patience_bars: int = 1,
    ) -> None:
        self.interval = interval
        self.size = size
        self.entry_offset = entry_offset
        self.patience_bars = patience_bars

        self._session = None
        self._entered = False
        self._limit_working = False
        self._bars_since_limit = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)

    def _place_limit(self, ctx) -> None:
        """Rest a limit buy if there is no position and nothing working."""
        if not ctx.actionable:
            return
        if self._entered or ctx.position or ctx.working_order_ids:
            return
        if self._limit_working:
            return

        price = ctx.last_price
        if not price or price <= 0:
            return

        limit = price - self.entry_offset
        if limit <= 0:
            limit = price * 0.995
        if limit <= 0:
            return

        order_id = ctx.buy(
            size=self.size,
            type="limit",
            limit=limit,
            tif="GTC",
            tag="entry",
        )
        if order_id is not None:
            self._limit_working = True
            self._bars_since_limit = 0

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_since_limit = 0

        # Once we own the position, hold to the end: no stops, no targets.
        if self._entered or ctx.position:
            return

        if self._limit_working:
            working = ctx.find_orders(tag="entry")
            if working:
                self._bars_since_limit += 1
                if self._bars_since_limit >= self.patience_bars:
                    for order in working:
                        ctx.cancel(order.id)
                    fallback_id = ctx.buy(
                        size=self.size,
                        tif="GTC",
                        tag="entry",
                    )
                    if fallback_id is not None:
                        self._limit_working = False
                        self._bars_since_limit = 0
                    return
            else:
                # The entry order is no longer working. If it filled, on_fill
                # set _entered and this callback would have returned above.
                self._limit_working = False
                self._bars_since_limit = 0
        else:
            self._place_limit(ctx)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position > 0:
            self._entered = True
            self._limit_working = False
            self._bars_since_limit = 0

    def on_lifecycle(self, ctx) -> None:
        kind = ctx.event.kind
        if kind == "session_open":
            self._bars_since_limit = 0
            self._place_limit(ctx)
        elif kind == "order_rejected":
            self._limit_working = False
            self._bars_since_limit = 0
            self._place_limit(ctx)
