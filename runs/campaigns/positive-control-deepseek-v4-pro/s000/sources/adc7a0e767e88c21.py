"""Limit entry with market fallback, held to the end of the run.

Prior trials in this campaign consistently show that turnover is expensive and
the best completed shape so far is a single fill with no exits.  This proposal
tries to improve on a pure market entry by first resting a small buy limit just
below the last price.  If price ticks down into it, the fill is better than a
market order.  If it does not fill quickly, the limit is cancelled and a GTC
market order is submitted, guaranteeing exposure without letting a stale DAY
limit leave the run without a position.

Once filled, the position is held until the end of the run: no exits, no
brackets, no churn.  The strategy keeps no order ids across callbacks; it finds
working orders by tag, which avoids stale-id errors when a DAY limit expires.

This is deliberately conservative.  Wall-clock and action-cap risk are minimal,
and the only working order at any moment is either the limit or the market
fallback, never both.
"""

from __future__ import annotations


class Strategy:
    LIMIT_TAG = "entry-limit"
    MARKET_TAG = "entry-market"

    def __init__(
        self,
        *,
        interval: str = "5m",
        limit_offset: float = 0.25,
        patience: int = 3,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.limit_offset = limit_offset
        self.patience = patience
        self.size = size

        self._entered = False
        self._limit_bars = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _submit_limit(self, ctx) -> None:
        if not ctx.actionable:
            return
        limit = ctx.last_price - self.limit_offset
        ctx.buy(
            size=self.size,
            type="limit",
            limit=limit,
            tif="DAY",
            tag=self.LIMIT_TAG,
        )
        self._limit_bars = 0

    def _submit_market(self, ctx) -> None:
        if not ctx.actionable:
            return
        ctx.buy(
            size=self.size,
            tif="GTC",
            tag=self.MARKET_TAG,
        )

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or self._entered:
            return

        if ctx.position:
            self._entered = True
            return

        # If the market fallback is already working, let it fill.
        if ctx.find_orders(tag=self.MARKET_TAG):
            return

        limit_orders = ctx.find_orders(tag=self.LIMIT_TAG)
        if limit_orders:
            self._limit_bars += 1
            if self._limit_bars >= self.patience:
                for order in limit_orders:
                    ctx.cancel(order.id)
                self._submit_market(ctx)
            return

        # Flat and nothing working: rest a limit below the market.
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        self._submit_limit(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            self._entered = True

    def on_lifecycle(self, ctx) -> None:
        if not ctx.actionable:
            return

        if self._entered or ctx.position:
            return

        if ctx.event.kind != "session_close":
            return

        # A session is ending while we are still flat.  Cancel any resting
        # limit so it does not simply expire and leave the run unexposed, then
        # place a GTC market order that fills at the next session's first tick.
        for order in ctx.find_orders(tag=self.LIMIT_TAG):
            ctx.cancel(order.id)

        if not ctx.find_orders(tag=self.MARKET_TAG):
            self._submit_market(ctx)
