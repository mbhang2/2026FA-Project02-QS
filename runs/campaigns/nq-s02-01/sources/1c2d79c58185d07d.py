"""Investigate whether an outsized move inside a session continues or gives back.

This variant combines the continuation idea with a time-of-day filter and a
more robust measure of outsizedness. Instead of using a fixed lookback median,
we compare the latest bar's range to the session's median range so far, but
require that the outsized bar occurs in the first half of the session. The
intuition: early outsized moves are more likely to reflect genuine information
arrival and repositioning, while late outsized moves are more likely to be
position-squaring or noise. We also require a minimum number of bars before
trading to ensure the median is stable.

Session state is reset on session boundaries, and all session-specific data is
kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an early outsized bar, measured against session median range."""

    def __init__(self, *, interval: str = "5m", min_bars: int = 10,
                 range_mult: float = 2.0, session_fraction: float = 0.5,
                 stop_mult: float = 1.0, target_mult: float = 1.5,
                 size: int = 1):
        self.interval = interval
        self.min_bars = min_bars
        self.range_mult = range_mult
        self.session_fraction = session_fraction
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._ranges: deque[float] = deque()

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

        # Only consider bars in the first fraction of the session.
        elapsed = ctx.ts - ctx.session_start
        session_length = ctx.session_end - ctx.session_start
        if session_length <= 0:
            return
        if elapsed > self.session_fraction * session_length:
            return

        self._ranges.append(rng)

        if len(self._ranges) < self.min_bars:
            return

        # Median of the session's ranges so far.
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
                    tag="early-outsized-continuation-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="early-outsized-continuation-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
