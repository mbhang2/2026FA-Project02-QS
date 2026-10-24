"""Patient long-only entry at a small discount, holding to the end.

The best prior result in this campaign came from a single GTC long market entry
with no exit: one position, no churn, continuous exposure.  This keeps that
shape and tries to improve the entry price.

Instead of buying immediately at the first actionable instant, we first rest a
GTC limit buy a small distance below the market.  If that limit has not filled
after a few bars, we cancel it and place a market order, so exposure is not
lost waiting forever.  This gives us a cheaper entry when the path dips, while
the fallback bounds how far the market can run away before we are in.
"""

from __future__ import annotations

from src.core.types import Side


class Strategy:
    """Long hold with a patient limit entry and a market fallback."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        size: int = 1,
        dip: float = 0.5,
        patience: int = 8,
    ):
        self.interval = interval
        self.size = size
        self.dip = dip
        self.patience = patience

        self._entered = False
        self._placed_limit = False
        self._bars_since_limit = 0

        self._session = None
        self._bars_this_session = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _initialize(self, ctx) -> None:
        """Rest a limit entry if there is nothing working and no position."""
        if not ctx.actionable:
            return
        if self._entered or ctx.position or ctx.working_order_ids:
            return
        if self._placed_limit:
            return

        price = ctx.last_price
        if not price or price <= 0:
            return

        limit = price - self.dip
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
            self._placed_limit = True
            self._bars_since_limit = 0

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
        self._bars_this_session += 1

        if self._entered or ctx.position:
            return

        if self._placed_limit:
            working = ctx.find_orders(tag="entry")
            if working:
                self._bars_since_limit += 1
                if self._bars_since_limit >= self.patience:
                    for order in working:
                        ctx.cancel(order.id)
                    # Replace the stale limit with a market order.  The queued
                    # cancel is applied before the queued market order.
                    fallback = ctx.buy(size=self.size, tif="GTC", tag="entry")
                    if fallback is not None:
                        self._placed_limit = False
                        self._bars_since_limit = 0
                    return
            else:
                # The limit is gone without a position.  Most likely it filled
                # and on_fill already handled it, but in any case reset state.
                self._placed_limit = False
                self._bars_since_limit = 0
        else:
            self._initialize(ctx)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.side == Side.BUY and fill.position > 0:
            self._entered = True
            self._placed_limit = False
            self._bars_since_limit = 0

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_open":
            self._initialize(ctx)
