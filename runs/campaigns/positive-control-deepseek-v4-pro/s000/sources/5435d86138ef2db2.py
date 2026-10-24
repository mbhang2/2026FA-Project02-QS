from __future__ import annotations

from typing import Optional


class Strategy:
    """Patient limit entry with fallback, then hold.

    Prior trials on this simulated dataset show a single fill held to the end
    scores best, and every extra round trip hurts. The only remaining place to
    improve is the entry price: a market order pays an unfavorable fill, while
    a GTC buy limit below the first trade of the session can buy a down tick.
    This strategy waits most of the first session for that discount, then
    cancels the limit and buys at market so exposure is not lost.
    """

    def __init__(
        self,
        *,
        interval: str = "30s",
        size: int = 1,
        entry_offset: float = 0.25,
        fallback_frac: float = 0.75,
    ) -> None:
        self.interval = interval
        self.size = size
        self.entry_offset = entry_offset
        self.fallback_frac = fallback_frac

        self._session: Optional[int] = None
        self._entered: bool = False
        self._reference_price: Optional[float] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)

    def _reset_session(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._reference_price = None

    def _observe_reference_price(self, ctx) -> None:
        if self._reference_price is not None:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        if bar.tick_count > 0:
            self._reference_price = float(bar.open)

    def _place_limit(self, ctx) -> None:
        if not ctx.actionable or self._entered or ctx.position:
            return
        if ctx.find_orders(tag="entry-limit") or ctx.find_orders(tag="entry-market"):
            return
        if self._reference_price is None:
            return

        limit = self._reference_price - self.entry_offset
        if limit <= 0:
            limit = self._reference_price * 0.995
        if limit <= 0:
            return

        ctx.buy(
            size=self.size,
            type="limit",
            limit=limit,
            tif="GTC",
            tag="entry-limit",
        )

    def _fallback_to_market(self, ctx) -> None:
        if not ctx.actionable or self._entered or ctx.position:
            return

        for order in ctx.find_orders(tag="entry-limit"):
            ctx.cancel(order.id)

        if not ctx.find_orders(tag="entry-market"):
            ctx.buy(size=self.size, tif="GTC", tag="entry-market")

    def on_bars(self, ctx) -> None:
        self._reset_session(ctx)

        if not ctx.actionable:
            return
        if self._entered or ctx.position:
            return

        self._observe_reference_price(ctx)

        total = ctx.session_end - ctx.session_start
        elapsed = ctx.ts - ctx.session_start
        fallback_after = int(total * self.fallback_frac) if total > 0 else 0
        near_fallback = total > 0 and elapsed >= fallback_after

        working_limit = ctx.find_orders(tag="entry-limit")
        working_market = ctx.find_orders(tag="entry-market")

        if working_limit:
            if near_fallback:
                self._fallback_to_market(ctx)
            return

        if working_market:
            return

        if near_fallback:
            self._fallback_to_market(ctx)
        else:
            self._place_limit(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position > 0:
            self._entered = True

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_open":
            self._reset_session(ctx)
