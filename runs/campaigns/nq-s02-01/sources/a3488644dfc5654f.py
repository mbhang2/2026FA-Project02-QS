"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #11 continuation + one-bar confirmation: +0.0838
- #12 continuation + two-bar confirmation + trend: +0.0994 (best)
- #13 added volatility regime filter: NOT SCORED (insufficient exposure)

#12 is the best so far. #13 added a volatility filter that reduced exposure
below the floor. This variant builds on #12 but addresses a potential weakness:
the two-bar confirmation may enter too late, missing the initial continuation.
Instead of requiring two full confirmation bars after the outsized bar, this
variant enters on the first confirmation bar but requires the outsized bar
itself to have a strong close (close in the top/bottom quartile of its range).
This captures continuation earlier while still filtering weak signals.

Key changes from #12:
1. Require only ONE confirmation bar after the outsized bar, but that bar
   must have a strong close in the direction of the move (top quartile for
   longs, bottom quartile for shorts).
2. The outsized bar itself must have a strong close (top/bottom quartile of
   its own range), not just close > open.
3. Keep the trend filter on the confirmation bar (higher high and higher low
   for longs, lower high and lower low for shorts).
4. Keep median-based outsized detection and bracket sizing from #12.

The hypothesis is that entering one bar earlier captures more of the
continuation move, while the stronger close requirements filter out the
false signals that the two-bar confirmation was designed to eliminate.

Session state is reset on session boundaries, and all session-specific data
is kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar with strong close, one confirmation."""

    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 range_mult: float = 2.0, close_quartile: float = 0.75,
                 stop_mult: float = 1.0, target_mult: float = 1.5,
                 size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.close_quartile = close_quartile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._ranges: deque[float] = deque(maxlen=lookback)
        self._pending_direction: int = 0  # 0=none, 1=long, -1=short
        self._pending_range: float = 0.0
        self._prev_bar = None  # previous bar for trend comparison

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
            self._prev_bar = None

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

        # If we have a pending signal, check for one confirmation bar.
        if self._pending_direction != 0:
            # Check if this bar confirms the direction.
            if self._pending_direction == 1:
                # For longs: close in top quartile of range, and higher high
                # and higher low than prev bar.
                close_pos = (bar.close - bar.low) / rng if rng > 0 else 0.0
                if close_pos >= self.close_quartile and (
                    self._prev_bar is None or
                    (bar.high > self._prev_bar.high and bar.low > self._prev_bar.low)
                ):
                    # Confirmed - enter.
                    stop = self._pending_range * self.stop_mult
                    target = self._pending_range * self.target_mult
                    ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                            tag="outsized-strong-close-confirmed-long")
                # Clear pending signal regardless.
                self._pending_direction = 0
                self._pending_range = 0.0
                self._prev_bar = bar
                return
            elif self._pending_direction == -1:
                # For shorts: close in bottom quartile of range, and lower high
                # and lower low than prev bar.
                close_pos = (bar.close - bar.low) / rng if rng > 0 else 0.0
                if close_pos <= (1.0 - self.close_quartile) and (
                    self._prev_bar is None or
                    (bar.high < self._prev_bar.high and bar.low < self._prev_bar.low)
                ):
                    # Confirmed - enter.
                    stop = self._pending_range * self.stop_mult
                    target = self._pending_range * self.target_mult
                    ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                             tag="outsized-strong-close-confirmed-short")
                # Clear pending signal regardless.
                self._pending_direction = 0
                self._pending_range = 0.0
                self._prev_bar = bar
                return

        # No pending signal - check for outsized bar.
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

        # Direction: the bar's own close versus open, with strong close.
        close_pos = (bar.close - bar.low) / rng if rng > 0 else 0.0
        if close_pos >= self.close_quartile:
            self._pending_direction = 1
        elif close_pos <= (1.0 - self.close_quartile):
            self._pending_direction = -1
        else:
            return  # Not a strong enough close to signal.

        self._pending_range = rng
        self._prev_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
