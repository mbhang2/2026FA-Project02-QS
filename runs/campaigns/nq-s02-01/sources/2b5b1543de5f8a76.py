"""Investigate whether an outsized move inside a session continues or gives back.

This variant measures outsizedness using the session's cumulative range but
enters in the direction of the move (continuation), unlike the prior fade
variant. The key difference from the original continuation strategy is that
the threshold is relative to the session's total realized range so far rather
than a rolling median of recent bars. We require a minimum number of bars
before trading to ensure the cumulative range is meaningful, and we use a
bracket sized off the outsized bar's range.

Session state is reset on session boundaries, and all session-specific data is
kept on self so the bounded window is never a problem.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation after an outsized bar, measured against cumulative session range."""

    def __init__(self, *, interval: str = "5m", min_bars: int = 5,
                 fraction: float = 0.25, stop_mult: float = 1.0,
                 target_mult: float = 1.5, size: int = 1):
        self.interval = interval
        self.min_bars = min_bars
        self.fraction = fraction
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._cum_range = 0.0
        self._bar_count = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset session state on a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._cum_range = 0.0
            self._bar_count = 0

        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        rng = bar.high - bar.low
        if rng <= 0:
            return

        # Update cumulative session range and bar count.
        self._cum_range += rng
        self._bar_count += 1

        if self._bar_count < self.min_bars:
            return

        # Outsized means this bar's range is a large fraction of the session's
        # total range so far.
        if rng < self.fraction * self._cum_range:
            return

        # Direction: the bar's own close versus open.
        if bar.close == bar.open:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > bar.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="cumulative-outsized-continuation-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="cumulative-outsized-continuation-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
