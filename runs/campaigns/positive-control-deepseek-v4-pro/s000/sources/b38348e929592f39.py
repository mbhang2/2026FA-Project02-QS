"""Patient limit entry, hold to the end of the run.

Prior trials show the best score so far came from the minimum-turnover hold
strategy: one entry, no exit, and continuous exposure afterwards.  This keeps
that structure but tries to improve the entry price.

Instead of buying at the first bar close, we rest a GTC buy limit a little
below it.  If the limit fills -- which is likely over a couple of sessions when
the price has any small downward excursion -- the entry is better than the
market order used before.  If it does not fill after a short grace period we
cancel it and place a market order, preserving exposure and the 70% exposure
floor.  The position is then held to the end of the run, paying commission on
exactly one entry fill and no exit fill.
"""

from __future__ import annotations


class Strategy:
    """Patient limit entry, fallback market, hold."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        entry_limit_offset: float = 0.5,
        max_wait_sessions: int = 2,
    ):
        self.interval = interval
        self.size = size
        self.entry_limit_offset = entry_limit_offset
        self.max_wait_sessions = max_wait_sessions

        self._started = False
        self._limit_session = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Already in the market; hold until the run ends.
        if ctx.position:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]

        working_limit = ctx.find_orders(tag="entry-limit")

        # First actionable bar: rest the patient buy limit.
        if not self._started:
            limit_price = last.close - self.entry_limit_offset
            order_id = ctx.buy(
                size=self.size,
                type="limit",
                limit=limit_price,
                tif="GTC",
                tag="entry-limit",
            )
            if order_id is not None:
                self._started = True
                self._limit_session = ctx.session
            return

        # The limit is still waiting.  If it has been waiting long enough,
        # cancel it and get into the market so the exposure floor is met.
        if working_limit:
            if (
                self._limit_session is not None
                and ctx.session > self._limit_session + self.max_wait_sessions
            ):
                ctx.cancel(working_limit[0].id)
                ctx.buy(
                    size=self.size,
                    tif="GTC",
                    tag="entry-market",
                )
            return

        # No working order and no position: make sure a market order is in
        # flight.  This retries any race from order rejection or action-cap
        # dropping, without changing the one-position/one-entry structure.
        if not ctx.working_order_ids:
            ctx.buy(
                size=self.size,
                tif="GTC",
                tag="entry-market",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
