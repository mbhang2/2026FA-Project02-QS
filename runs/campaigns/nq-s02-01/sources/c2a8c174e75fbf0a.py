"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #6 continuation + vol filter: +0.0166
- #7 give-back: -0.0690
- #8 continuation + vol filter + time-of-day: -0.0054
- #9 continuation + trend + breakout: +0.0095
- #10 continuation + percentile: -0.0527
- #11 continuation + one-bar confirmation: +0.0838 (best)

The one-bar confirmation in #11 was a strong improvement, suggesting that
waiting for the next bar to confirm the direction filters out many false
signals. This variant builds on #11 by adding a second confirmation bar
and a trend filter on the confirmation bars themselves.

Key changes from #11:
1. Require two consecutive confirmation bars in the same direction after
   the outsized bar, not just one. This further reduces false signals.
2. Add a trend filter: the confirmation bars must have higher highs and
   higher lows for longs (lower highs and lower lows for shorts) to ensure
   genuine momentum, not just a small positive close.
3. Keep the median-based outsized detection and bracket sizing from #11.

The two-bar confirmation introduces a slightly longer delay, but the entry
is still early enough to capture continuation if it exists. The bracket is
sized from the outsized bar's range for consistent risk.

Session state is reset on session boundaries, and all session-specific data
is kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar, confirmed by two trend bars."""

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
        self._confirmation_count: int = 0
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
            self._confirmation_count = 0
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

        # If we have a pending signal, check for two consecutive confirmation bars.
        if self._pending_direction != 0:
            # Check if this bar confirms the direction.
            if self._pending_direction == 1:
                # For longs: close > open, and higher high and higher low than prev bar.
                if bar.close > bar.open and (
                    self._prev_bar is None or
                    (bar.high > self._prev_bar.high and bar.low > self._prev_bar.low)
                ):
                    self._confirmation_count += 1
                else:
                    # Failed confirmation - reset.
                    self._pending_direction = 0
                    self._pending_range = 0.0
                    self._confirmation_count = 0
                    self._prev_bar = bar
                    return
            elif self._pending_direction == -1:
                # For shorts: close < open, and lower high and lower low than prev bar.
                if bar.close < bar.open and (
                    self._prev_bar is None or
                    (bar.high < self._prev_bar.high and bar.low < self._prev_bar.low)
                ):
                    self._confirmation_count += 1
                else:
                    # Failed confirmation - reset.
                    self._pending_direction = 0
                    self._pending_range = 0.0
                    self._confirmation_count = 0
                    self._prev_bar = bar
                    return

            self._prev_bar = bar

            # Need two consecutive confirmation bars.
            if self._confirmation_count >= 2:
                stop = self._pending_range * self.stop_mult
                target = self._pending_range * self.target_mult
                if self._pending_direction == 1:
                    ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                            tag="outsized-two-bar-confirmed-long")
                else:
                    ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                             tag="outsized-two-bar-confirmed-short")
                # Clear pending signal.
                self._pending_direction = 0
                self._pending_range = 0.0
                self._confirmation_count = 0
                self._prev_bar = None
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

        # Direction: the bar's own close versus open.
        if bar.close == bar.open:
            return

        # Set pending signal for next bars.
        if bar.close > bar.open:
            self._pending_direction = 1
        else:
            self._pending_direction = -1
        self._pending_range = rng
        self._confirmation_count = 0
        self._prev_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
