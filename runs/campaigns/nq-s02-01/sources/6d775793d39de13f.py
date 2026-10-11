"""Investigate whether an outsized move inside a session continues or gives back.

This variant trades the give-back hypothesis. When an outsized bar occurs, we
fade it: if the bar closed up, we sell short; if it closed down, we buy. The
idea is that a large move often reflects a temporary imbalance that will revert
as the session continues. We require the outsized bar to be large relative to
the session's median range, and we use a bracket with offsets scaled to the
bar's range.

Session state is reset on session boundaries, and all session-specific data is
kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade mean reversion after an outsized bar."""

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

        # Keep the session's ranges on self.
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

        # Fade the move: if the bar closed up, sell short; if down, buy.
        if bar.close > bar.open:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-giveback-short")
        else:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-giveback-long")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
