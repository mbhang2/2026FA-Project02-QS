"""Minimal-turnover hold strategy.

Prior trials in this campaign all lost money, and the best score so far was
achieved by cutting trade count from over 1000 to 133.  The documented facts
are that this run has an unfavorable fill model and charges commission on every
fill, so turnover itself is a measurable cost.

A no-trade strategy was refused for failing the 70% exposure floor, so this
strategy takes the opposite extreme: buy once and hold.  It obtains continuous
market exposure in every scored session while paying commission and adverse
fill cost on exactly one entry fill.  With no information edge, minimizing
transaction cost is the only reliable way to raise the score.
"""


class Strategy:
    """Buy once and hold to the end of the run."""

    def __init__(self, *, interval: str = "5m", size: int = 1):
        self.interval = interval
        self.size = size
        self._entered = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if self._entered or ctx.position or ctx.working_order_ids:
            return

        order_id = ctx.buy(
            size=self.size,
            tif="GTC",
            tag="hold",
        )
        if order_id is not None:
            self._entered = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
