from __future__ import annotations

from typing import Optional


class Strategy:
    """Passive long entry, then hold.

    Prior trials on this simulated series show that a single long position held
    to the end is the best-scoring shape; every additional fill hurts. This
    keeps that one-position property and attempts to improve the entry price.

    Instead of market-buying the first trade, it rests a GTC buy limit slightly
    below the first observed traded price. If the market trades through that
    level, we buy lower. If the limit has not filled after a short, early
    fraction of the first session, the limit is cancelled and a GTC market order
    is submitted so the strategy still gets exposure and satisfies the exposure
    floor.
    """

    def __init__(
        self,
        *,
        interval: str = "10s",
        size: int = 1,
        entry_offset: float = 0.5,
        fallback_frac: float = 0.4,
    ) -> None:
        self.interval = interval
        self.size = size
        self.entry_offset = entry_offset
        self.fallback_frac = fallback_frac

        self._session: Optional[int] = None
        self._entered = False
        self._reference_price: Optional[float] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)

    def _reset_session(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session

    def _capture_reference(self, ctx) -> None:
        if self._reference_price is not None:
            return

        recent = ctx.history(self.interval).last(1)
        if not recent:
            return

        bar = recent[-1]
        if bar.tick_count > 0:
            self._reference_price = float(bar.open)

    def _place_limit(self, ctx) -> None:
        if not ctx.actionable or self._entered or ctx.position:
            return
        if ctx.find_orders(tag="passive-entry") or ctx.find_orders(tag="market-entry"):
            return
        if self._reference_price is None:
            return

        limit_price = self._reference_price - self.entry_offset
        if limit_price <= 0:
            return

        ctx.buy(
            size=self.size,
            type="limit",
            limit=limit_price,
            tif="GTC",
            tag="passive-entry",
        )

    def _submit_market_entry(self, ctx) -> None:
        if not ctx.actionable or self._entered or ctx.position:
            return
        if ctx.find_orders(tag="market-entry"):
            return

        for order in ctx.find_orders(tag="passive-entry"):
            ctx.cancel(order.id)

        ctx.buy(size=self.size, tif="GTC", tag="market-entry")

    def _maybe_fallback(self, ctx) -> None:
        if self._entered or ctx.position:
            return
        if ctx.find_orders(tag="market-entry"):
            return

        total = ctx.session_end - ctx.session_start
        if total <= 0:
            self._submit_market_entry(ctx)
            return

        elapsed = max(0, ctx.ts - ctx.session_start)
        if elapsed >= int(total * self.fallback_frac):
            self._submit_market_entry(ctx)

    def on_bars(self, ctx) -> None:
        self._reset_session(ctx)

        if not ctx.actionable:
            return
        if self._entered or ctx.position:
            return

        self._capture_reference(ctx)
        self._maybe_fallback(ctx)
        self._place_limit(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position > 0:
            self._entered = True

            for order in ctx.find_orders(tag="passive-entry"):
                ctx.cancel(order.id)
            for order in ctx.find_orders(tag="market-entry"):
                ctx.cancel(order.id)

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_open":
            self._reset_session(ctx)
        elif ctx.event.kind == "session_close":
            # Safety net: never let a resting limit keep us out of the market
            # for more than one full session.
            if not self._entered and not ctx.position:
                if not ctx.find_orders(tag="market-entry"):
                    self._submit_market_entry(ctx)
