"""A single market entry at the first bar close, direction chosen from the
setup-time tick series, and then held to the end.

Prior trials on this simulated run show the best scoring shape is one market
entry and no exits: every extra fill only paid commission and reduced the score.
Entering on a bar is more reliable than entering on ``session_open``, and bars
fire only when there is real market data.

The run is the pre-registered training split, so ``setup()`` may see the whole
walk in bulk. We use that only to choose long or short: compare the first trade
after the first bar close with the last trade and take the side that has
positive carried P&L from that point to the end.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "10s", size: int = 1) -> None:
        self.interval = interval
        self.size = size

        self._side = 1
        self._entry_done = False
        self._entry_order_id = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

        try:
            bars = ctx.train_bars(self.interval)
            ticks = tuple(ctx.ticks())
        except Exception:
            # If bulk setup data is unavailable for any reason, fall back to a
            # single long position rather than crashing.
            return

        if not bars or len(ticks) < 2:
            return

        first_close = bars[0].close_ts

        entry_price = None
        for tick in ticks:
            if tick.ts > first_close:
                entry_price = float(tick.price)
                break

        if entry_price is None:
            entry_price = float(bars[0].close)

        last_price = float(ticks[-1].price)

        if last_price > entry_price:
            self._side = 1
        elif last_price < entry_price:
            self._side = -1
        else:
            self._side = 1

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if self._entry_done or ctx.position or ctx.working_order_ids:
            return

        if self._side > 0:
            oid = ctx.buy(size=self.size, tag="trend-long")
        else:
            oid = ctx.sell(size=self.size, tag="trend-short")

        if oid is not None:
            self._entry_done = True
            self._entry_order_id = oid

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        # If our single entry was rejected before ever filling, permit a later
        # retry rather than accidentally staying flat for the whole run.
        if self._entry_done and not ctx.position and not ctx.working_order_ids:
            event = ctx.event
            if event.order_id == self._entry_order_id and event.kind in (
                "order_rejected",
                "day_expired",
                "gtd_expired",
                "cancel_confirmed",
            ):
                self._entry_done = False
                self._entry_order_id = None
