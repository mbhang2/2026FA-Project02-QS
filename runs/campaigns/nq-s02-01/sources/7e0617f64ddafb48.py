"""Investigate whether an outsized move inside a session continues or gives back.

Measure "large" against the session's own realised range so far: compute the
rolling range of the last N closed 5m bars within the current session, and treat
a move as outsized when the latest bar's range (high-low) exceeds a multiple of
the median of the last N ranges. Enter in the direction of the outsized bar's
close relative to its open, with a bracket sized off the bar's range. This is a
continuation bet: if the give-back account is right, this will lose.

Session state is reset on session boundaries, and history is kept on self so the
bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar, measured against session range."""

    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 range_mult: float = 2.0, stop_mult: float = 1.0,
                 target_mult: float = 1.5, size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._ranges: deque[float] = deque(maxlen=lookback)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset session state on a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()

        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        rng = bar.high - bar.low
        if rng <= 0:
            return

        # Keep the session's ranges on self so the bounded window is irrelevant.
        self._ranges.append(rng)

        if len(self._ranges) < self.lookback:
            return

        # Median of the session's recent ranges.
        ordered = sorted(self._ranges)
        mid = len(ordered) // 2
        if len(ordered) % 2 == 0:
            median = (ordered[mid - 1] + ordered[mid]) / 2.0
        else:
            median = float(ordered[mid])
        if median <= 0:
            return

        # Outsized means the latest bar's range is a large multiple of the
        # session's typical range so far.
        if rng < median * self.range_mult:
            return

        # Direction: the bar's own close versus open.
        if bar.close == bar.open:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > bar.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-continuation-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-continuation-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
