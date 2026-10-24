"""One-shot momentum at the first usable 1-second bar close, with no exit.

Prior trials consistently show that turnover and repeated bracketed exits are
expensive.  Attempts to enter once from lifecycle events (#11, #29) did not
fill, likely because the entry was queued before the first regular bar/tick
path on which market orders have been observed to fill in this harness.

This version submits one market order from the first 1-second bar close, which
is early in every session and well before the session close.  The side is
chosen by the signal in the first closed bar: up -> long, down -> short.  The
order is a plain DAY market order, so if it ever failed to fill before the
session close it would expire and the next session would simply try again.

Once filled, the strategy holds the position to the end of the run.  No stops,
no targets, no manual exits -- one fill and continuous exposure.
"""

from __future__ import annotations


class Strategy:
    """First-bar momentum, one trade, held to the end."""

    def __init__(self, *, interval: str = "1s", size: int = 1) -> None:
        self.interval = interval
        self.size = size
        self._entered = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if self._entered or ctx.position:
            self._entered = True
            return

        # Do not duplicate an entry that is already working.
        if ctx.find_orders(tag="one-shot-momentum"):
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        if bar.close >= bar.open:
            ctx.buy(size=self.size, tag="one-shot-momentum")
        else:
            ctx.sell(size=self.size, tag="one-shot-momentum")

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            self._entered = True

    def on_lifecycle(self, ctx) -> None:
        # Intentionally no stored order ids.  If a DAY entry expires before
        # filling, find_orders() will be empty next bar and on_bars() will
        # place a fresh one.  Once filled, on_fill() marks us entered.
        pass
