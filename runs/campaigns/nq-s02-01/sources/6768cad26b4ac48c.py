"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #6 continuation + vol filter: +0.0166 (best)
- #7 give-back: -0.0690
- #8 continuation + vol filter + time-of-day: -0.0054
- #9 continuation + trend + breakout: +0.0095

The continuation hypothesis remains the best-supported direction, but the
filters tried so far have been inconsistent. The volatility filter in #6 helped,
but the time-of-day filter in #8 hurt, and the trend/breakout filters in #9 were
neutral to slightly negative.

This variant takes a different approach to the same question: instead of
requiring the outsized bar to be confirmed by other conditions, we focus on
the *magnitude* of the outsized move relative to the session's recent range.
We use a percentile-based threshold rather than a simple multiple of the median,
which should be more robust to the distribution of bar ranges. Additionally,
we use a longer lookback for the baseline range (30 bars instead of 20) to get
a more stable estimate of "typical" volatility.

The key insight is that the continuation effect may be strongest when the
outsized bar is *truly* exceptional, not just moderately above average. By
using a higher percentile threshold (e.g., 90th percentile of recent ranges),
we focus on the most extreme moves, which are more likely to reflect genuine
information flow rather than noise.

We also add a small confirmation requirement: the outsized bar's close must be
in the direction of the move (close > open for longs, close < open for shorts),
but we do not require a breakout beyond the prior bar's high/low, as that may
have been too restrictive in #9.

Session state is reset on session boundaries, and all session-specific data is
kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an exceptionally outsized bar, using percentile threshold."""

    def __init__(self, *, interval: str = "5m", lookback: int = 30,
                 range_percentile: float = 90.0, stop_mult: float = 1.0,
                 target_mult: float = 1.5, size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.range_percentile = range_percentile
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

        # Compute the percentile threshold from the recent ranges.
        ordered = sorted(self._ranges)
        idx = int((self.range_percentile / 100.0) * (len(ordered) - 1))
        threshold = float(ordered[idx])
        if threshold <= 0:
            return

        # Outsized means the latest bar's range is above the percentile
        # threshold of recent ranges.
        if rng < threshold:
            return

        # Direction: the bar's own close versus open.
        if bar.close == bar.open:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > bar.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-percentile-continuation-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-percentile-continuation-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
