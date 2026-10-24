"""Next proposal: adaptive-direction minimal-turnover hold.

Prior trials show that active strategies bleed score through turnover and
commission, and the best scores so far come from a single GTC entry with no
exit. This strategy keeps that one-fill/no-round-trip structure, but instead of
always buying or always selling it waits for a small number of completed daily
sessions and enters in the direction of that early cumulative move. This is a
pure-past rule: it uses only bars that have already closed at the decision
instant. With lookback_sessions=5 it is exposed for roughly 95 of 105 sessions,
comfortably above the 70% exposure floor, and it pays commission on exactly one
fill while holding to the end of the run.
"""

from __future__ import annotations


class Strategy:
    """One early entry in the direction of the first few daily bars, then hold."""

    def __init__(
        self,
        interval: str = "5m",
        daily: str = "1d",
        lookback_sessions: int = 5,
        size: int = 1,
    ):
        self.interval = interval
        self.daily = daily
        self.lookback_sessions = lookback_sessions
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily)

    def on_bars(self, ctx) -> None:
        # Only act when a fast bar closes; a daily bar closing is not the
        # decision point by itself.
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        daily = ctx.history(self.daily)
        if len(daily) < self.lookback_sessions:
            return

        window = daily.last(self.lookback_sessions)
        move = window[-1].close - window[0].open

        # Enter once, in the direction of the first completed sessions, and
        # hold until the end of the run. GTC keeps the order alive across
        # sessions if it does not fill immediately.
        if move >= 0:
            ctx.buy(size=self.size, tif="GTC", tag="adaptive-hold-long")
        else:
            ctx.sell(size=self.size, tif="GTC", tag="adaptive-hold-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
