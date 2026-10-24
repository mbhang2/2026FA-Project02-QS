"""Fade the first few completed sessions, then hold.

Prior trials show that a one-way exposure with no exit has been the least bad
way to meet the 70% exposure floor: extra round trips only pay commission with
no edge in the underlying. Trial #6 (patient long, hold) scored -0.0976.
Trial #7 entered in the *direction* of the first five completed daily bars and
scored worse (-0.2207), consistent with that early move reverting rather than
continuing. This proposal keeps the same one-fill, hold-to-end structure but
takes the opposite side after the same five-session lookback: sell into an
early up-move, buy into an early down-move. One entry, no exit, exposure for
almost the entire scored period.
"""

from __future__ import annotations


class Strategy:
    """Early-session fade, held to the end of the run."""

    def __init__(
        self,
        *,
        interval: str = "1d",
        lookback_sessions: int = 5,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback_sessions = lookback_sessions
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        daily = ctx.history(self.interval)
        if len(daily) < self.lookback_sessions:
            return

        window = daily.last(self.lookback_sessions)
        move = window[-1].close - window[0].open

        if move > 0:
            ctx.sell(size=self.size, tif="GTC", tag="fade-short")
        else:
            # Down move, or a flat first five sessions: get long and stay
            # there. One direction has to be chosen so the exposure floor
            # is met; no exit is placed.
            ctx.buy(size=self.size, tif="GTC", tag="fade-long")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
