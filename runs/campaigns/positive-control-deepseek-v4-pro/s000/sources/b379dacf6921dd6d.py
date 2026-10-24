"""Momentum with volatility-scaled bracket.

Keeps the same bar-direction signal as the reference strategy but replaces
fixed 0.75 offsets with an average true range computed from recent closed
bars.  ATR-scaling lets the bracket adapt when volatility changes and gives
the winners a little more room than the stop.  The strategy uses 5m bars,
which are cheap and fit the wall-clock budget; no tick subscription.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", atr_lookback: int = 10,
                 stop_mult: float = 1.0, target_mult: float = 1.5,
                 size: int = 1):
        self.interval = interval
        self.atr_lookback = atr_lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.atr_lookback + 1:
            return

        window = history.last(self.atr_lookback + 1)
        if len(window) < self.atr_lookback + 1:
            return

        last = window[-1]
        prior = window[:-1]

        # Average true range over completed bars before the one that just closed.
        atr = sum(b.high - b.low for b in prior) / len(prior)
        if atr <= 0:
            return

        stop_offset = atr * self.stop_mult
        target_offset = atr * self.target_mult

        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="momentum-long",
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="momentum-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
