"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a continuation hypothesis with a different trigger than the
prior attempts. Instead of requiring the move to be sustained over multiple bars,
it enters immediately when the session's range expands sharply: the current bar's
range is at least a multiple of the average range of the preceding bars in the
session. The idea is that a sudden expansion of volatility, when the session
range is still small relative to the move, marks the start of a directional
impulse that continues.

The entry is a market order with a bracket. The stop is wider than the target to
give the continuation room to breathe while still capping losses.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of a sudden volatility expansion inside a session.

    The trigger is a bar whose range is large relative to the average range of
    the bars seen so far in the session. The direction is the direction of the
    current bar's close relative to its open. The session range itself is not
    used, avoiding the lookahead issue of measuring the move against the full
    session range.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        range_multiple: float = 2.5,
        min_bars: int = 5,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 1.0,
    ):
        self.interval = interval
        self.range_multiple = range_multiple
        self.min_bars = min_bars
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        # Session-level state, kept across callbacks.
        self._session = None
        self._ranges: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]

        # Reset state on session boundary.
        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges = []

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return

        # Need a baseline of recent ranges before the trigger is meaningful.
        self._ranges.append(bar_range)
        if len(self._ranges) < self.min_bars:
            return

        # Average range of the bars before the current one.
        prior_ranges = self._ranges[:-1]
        avg_range = sum(prior_ranges) / len(prior_ranges)
        if avg_range <= 0:
            return

        if bar_range < self.range_multiple * avg_range:
            return

        # Direction is the bar's own direction.
        move = bar.close - bar.open
        if move > 0:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="vol-expansion-long",
            )
        elif move < 0:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="vol-expansion-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
