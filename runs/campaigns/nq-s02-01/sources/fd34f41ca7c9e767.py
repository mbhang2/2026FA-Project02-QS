"""Investigate whether an outsized move inside a session continues or gives back.

This variant combines the continuation hypothesis with a volatility regime filter.
The outsized bar must exceed a multiple of the session's median range, but we
only take continuation trades when the session's realized volatility is elevated
relative to its own recent history. This aims to capture continuation moves that
occur during genuine information-driven volatility expansion, while avoiding
fades during quiet consolidation.

Session state is reset on session boundaries, and all session-specific data is
kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar during elevated volatility."""

    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 range_mult: float = 2.0, vol_lookback: int = 30,
                 vol_mult: float = 1.5, stop_mult: float = 1.0,
                 target_mult: float = 1.5, size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.vol_lookback = vol_lookback
        self.vol_mult = vol_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._ranges: deque[float] = deque(maxlen=lookback)
        self._vol_ranges: deque[float] = deque(maxlen=vol_lookback)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset session state on a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()
            self._vol_ranges.clear()

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
        self._vol_ranges.append(rng)

        if len(self._ranges) < self.lookback:
            return

        # Median of the session's recent ranges for outsized detection.
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

        # Volatility regime filter: current median range over vol_lookback
        # must be elevated relative to the session's overall median range.
        if len(self._vol_ranges) >= self.vol_lookback:
            vol_ordered = sorted(self._vol_ranges)
            vol_mid = len(vol_ordered) // 2
            if len(vol_ordered) % 2 == 0:
                vol_median = (vol_ordered[vol_mid - 1] + vol_ordered[vol_mid]) / 2.0
            else:
                vol_median = float(vol_ordered[vol_mid])
            if vol_median > 0 and median > vol_median * self.vol_mult:
                return

        # Direction: the bar's own close versus open.
        if bar.close == bar.open:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > bar.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-vol-continuation-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-vol-continuation-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
