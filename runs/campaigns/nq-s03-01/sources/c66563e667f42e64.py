"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a continuation hypothesis with a different trigger than all
prior attempts. Instead of measuring the move from the session open, it measures
the move over a fixed number of recent bars. The idea is that a large move over
a short, recent window is more likely to be a genuine impulse than a move from
the open, which can be large simply because the session has trended gradually.
The trigger requires the current bar to close beyond the high (for longs) or low
(for shorts) of the preceding N bars, and the bar's range to be large relative
to the average range of those bars. This is a breakout with a volatility filter,
entering on the close of the breakout bar.

The entry is a market order with a bracket. The stop is wider than the target
to give the continuation room to breathe while still capping losses.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of a breakout from a recent consolidation.

    A breakout occurs when the current bar closes beyond the high or low of the
    preceding lookback bars. The breakout bar must also have a range that is at
    least a multiple of the average range of those preceding bars, confirming
    that the move is outsized relative to recent volatility.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 10,
        range_multiple: float = 1.5,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 1.0,
    ):
        self.interval = interval
        self.lookback = lookback
        self.range_multiple = range_multiple
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        # The current bar is the last one; the preceding bars are the lookback.
        current = history[-1]
        prior = history[-self.lookback - 1 : -1]

        if not prior:
            return

        prior_high = max(b.high for b in prior)
        prior_low = min(b.low for b in prior)

        prior_ranges = [b.high - b.low for b in prior]
        avg_range = sum(prior_ranges) / len(prior_ranges)
        if avg_range <= 0:
            return

        current_range = current.high - current.low
        if current_range < self.range_multiple * avg_range:
            return

        # Breakout above the prior high: continuation long.
        if current.close > prior_high:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="breakout-continuation-long",
            )
        # Breakout below the prior low: continuation short.
        elif current.close < prior_low:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="breakout-continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
