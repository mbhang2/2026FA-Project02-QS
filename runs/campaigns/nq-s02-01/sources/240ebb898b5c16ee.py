"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #11 continuation + one-bar confirmation: +0.0838
- #12 continuation + two-bar confirmation + trend: +0.0994 (best)
- #13 added volatility regime filter: NOT SCORED (insufficient exposure)
- #14 strong close + one confirmation: -0.0245

#12 remains the best. #14's earlier entry with strong close requirements
hurt performance, suggesting the two-bar confirmation in #12 is genuinely
filtering false signals. This variant builds on #12 but adds a session-level
trend filter: only take continuation signals in the direction of the
session's overall trend, measured from the session open to the outsized bar.

Key changes from #12:
1. Compute session trend as the sign of (current price - session open price).
2. Only take outsized signals that align with the session trend.
3. Keep the two-bar confirmation with trend from #12 unchanged.

The hypothesis is that outsized moves are more likely to continue when they
align with the session's dominant direction, as this indicates the move is
part of a larger flow rather than an isolated spike.

Session state is reset on session boundaries, and all session-specific data
is kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar in the session trend direction."""

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
        self._session_open_price: float | None = None
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
            self._session_open_price = None
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

        # Track the session open price (first bar's open).
        if self._session_open_price is None:
            self._session_open_price = bar.open

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
                            tag="outsized-trend-confirmed-long")
                else:
                    ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                             tag="outsized-trend-confirmed-short")
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

        # Determine session trend from open to current bar's close.
        if self._session_open_price is None:
            return
        session_trend = 1 if bar.close > self._session_open_price else -1

        # Only take signals that align with the session trend.
        if bar.close > bar.open:
            if session_trend != 1:
                return
            self._pending_direction = 1
        else:
            if session_trend != -1:
                return
            self._pending_direction = -1

        self._pending_range = rng
        self._confirmation_count = 0
        self._prev_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
