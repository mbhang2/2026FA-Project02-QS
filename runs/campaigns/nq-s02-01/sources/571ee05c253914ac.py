"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #6 continuation + vol filter: +0.0166 (best)
- #7 give-back: -0.0690
- #8 continuation + vol filter + time-of-day: -0.0054
- #9 continuation + trend + breakout: +0.0095
- #10 continuation + percentile: -0.0527

The continuation hypothesis has been the only direction with positive scores,
but the filters have been inconsistent. The volatility filter in #6 helped,
but the percentile approach in #10 was disastrous, suggesting that focusing
on the most extreme moves is not the right approach — perhaps those extreme
moves are more likely to revert.

This variant returns to the core of #6 but simplifies the volatility filter
and adds a mean-reversion check on the outsized bar itself. Instead of
requiring elevated volatility, we require that the outsized bar is NOT
followed by immediate reversal — we wait for the next bar to confirm
continuation before entering. This should reduce false signals where the
outsized bar is a spike that immediately retraces.

Key changes from #6:
1. Drop the volatility regime filter entirely — it added complexity but
   the evidence is mixed.
2. Add a one-bar confirmation: after an outsized bar, wait for the next
   bar to close in the same direction before entering. This is a simple
   momentum confirmation that should filter out one-bar spikes.
3. Keep the median-based outsized detection from #6, which was the best
   performer.

The confirmation bar introduces a one-bar delay, but the entry is still
early enough to capture continuation if it exists. The bracket is sized
from the outsized bar's range, not the confirmation bar's range, to
maintain a consistent risk profile.

Session state is reset on session boundaries, and all session-specific data
is kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar, confirmed by the next bar."""

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
        self._pending_direction: int = 0  # 0=none, 1=long, -1=short
        self._pending_range: float = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset session state on a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()
            self._pending_direction = 0
            self._pending_range = 0.0

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

        # If we have a pending signal from the previous bar, check confirmation.
        if self._pending_direction != 0:
            if self._pending_direction == 1 and bar.close > bar.open:
                # Confirmed long continuation.
                stop = self._pending_range * self.stop_mult
                target = self._pending_range * self.target_mult
                ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                        tag="outsized-confirmed-continuation-long")
            elif self._pending_direction == -1 and bar.close < bar.open:
                # Confirmed short continuation.
                stop = self._pending_range * self.stop_mult
                target = self._pending_range * self.target_mult
                ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                         tag="outsized-confirmed-continuation-short")
            # Clear pending signal regardless of confirmation.
            self._pending_direction = 0
            self._pending_range = 0.0
            return

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

        # Set pending signal for next bar.
        if bar.close > bar.open:
            self._pending_direction = 1
        else:
            self._pending_direction = -1
        self._pending_range = rng

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
