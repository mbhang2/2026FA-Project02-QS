"""Investigate whether an outsized move inside a session continues or gives back.

This variant tests the give-back hypothesis directly: after an outsized move,
fade it. The outsized move is defined as a bar whose range is a multiple of the
session's median range so far, and we enter opposite the bar's direction. The
bracket is sized off the bar's range. This is the mirror of the continuation
strategy that scored best so far, and directly tests whether the effect is
continuation or give-back.

Session state is reset on session boundaries, and history is kept on self so the
bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade give-back after an outsized bar, measured against session range."""

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

        # Direction: fade the bar's own close versus open.
        if bar.close == bar.open:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > bar.open:
            # Bar was up; fade it by selling.
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-giveback-short")
        else:
            # Bar was down; fade it by buying.
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-giveback-long")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
