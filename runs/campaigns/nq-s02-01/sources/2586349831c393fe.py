"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #6 continuation + vol filter: +0.0166 (best)
- #7 give-back: -0.0690
- #8 continuation + vol filter + time-of-day: -0.0054

The give-back hypothesis performed very poorly, while continuation with a
volatility filter showed promise. #8 added a time-of-day filter but that hurt,
possibly because it excluded too many trades or the filter was mis-specified.

This variant refines the continuation approach by:
1. Using a more robust outsized detection: comparing the latest bar's range to
   the session's median range over a longer lookback, but requiring the outsized
   bar to be in the direction of the session's prevailing trend (measured by
   cumulative delta over the session).
2. Adding a confirmation filter: the outsized bar's close must be beyond the
   prior bar's high (for longs) or low (for shorts), ensuring the move actually
   broke out rather than just being a wide bar.
3. Keeping the volatility regime filter but simplifying it: instead of comparing
   two medians, we require the current bar's range to be above the session's
   median range, which is already implied by the outsized condition. The
   volatility filter is dropped to avoid over-filtering.

The trend filter is intended to capture continuation moves that align with the
session's order flow, while the breakout confirmation avoids fading moves that
are merely wide but not directional.

Session state is reset on session boundaries, and all session-specific data is
kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized breakout bar aligned with session trend."""

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
        self._session_delta: float = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset session state on a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()
            self._session_delta = 0.0

        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]
        rng = bar.high - bar.low
        if rng <= 0:
            return

        # Accumulate session delta from each bar's delta.
        self._session_delta += bar.delta

        # Keep the session's ranges on self.
        self._ranges.append(rng)

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

        # Direction: the bar's own close versus open.
        if bar.close == bar.open:
            return

        # Trend filter: session cumulative delta must agree with the bar's
        # direction.
        if bar.close > bar.open and self._session_delta <= 0:
            return
        if bar.close < bar.open and self._session_delta >= 0:
            return

        # Breakout confirmation: the close must be beyond the prior bar's high
        # for longs, or below the prior bar's low for shorts.
        if len(history) < 2:
            return
        prev_bar = history[-2]
        if bar.close > bar.open and bar.close <= prev_bar.high:
            return
        if bar.close < bar.open and bar.close >= prev_bar.low:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > bar.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-trend-breakout-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-trend-breakout-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
