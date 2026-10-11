"""Investigate whether an outsized move inside a session continues or gives back.

This variant refines the continuation hypothesis by requiring both an outsized
bar and a directional bias from the session's cumulative delta. The outsized
bar must exceed a multiple of the session's median range, and the bar's
direction must agree with the sign of cumulative delta over the recent bars.
This filters continuation signals to those aligned with the session's order
flow, reducing noise and improving the odds that the move is information-driven
rather than a temporary imbalance.

Session state is reset on session boundaries, and all session-specific data is
kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar aligned with session delta."""

    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 range_mult: float = 2.0, delta_lookback: int = 10,
                 stop_mult: float = 1.0, target_mult: float = 1.5,
                 size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.delta_lookback = delta_lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._ranges: deque[float] = deque(maxlen=lookback)
        self._deltas: deque[float] = deque(maxlen=delta_lookback)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset session state on a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()
            self._deltas.clear()

        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        rng = bar.high - bar.low
        if rng <= 0:
            return

        # Keep the session's ranges and deltas on self.
        self._ranges.append(rng)
        self._deltas.append(bar.delta)

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

        # Compute cumulative delta over the delta lookback window.
        cum_delta = sum(self._deltas)
        if cum_delta == 0:
            return

        # Require the bar direction to agree with cumulative delta.
        if bar.close > bar.open and cum_delta <= 0:
            return
        if bar.close < bar.open and cum_delta >= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > bar.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-delta-continuation-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-delta-continuation-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
