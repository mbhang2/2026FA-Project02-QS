"""Return to the fixed-offset momentum baseline.

The previous revision replaced fixed 0.75 offsets with ATR-scaled ones and
finished at -0.4287 with 1088 fills.  The planted signal is a one-bar
direction signal; the original fixed bracket is the cleanest expression of it
and avoids the extra parameter risk that the ATR variant introduced.  Trade
one contract at a time, and let the engine's bracket manage exits from the
instant the entry fills.
"""

from __future__ import annotations


class Strategy:
    """Fixed-offset bar-direction momentum."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        stop: float = 0.75,
        target: float = 0.75,
        size: int = 1,
    ):
        self.interval = interval
        self.stop = stop
        self.target = target
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]
        if last.close == last.open:
            return

        place = ctx.buy if last.close > last.open else ctx.sell
        place(
            size=self.size,
            stop_offset=self.stop,
            target_offset=self.target,
            tag="entry",
        )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
