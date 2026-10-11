"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #11 continuation + one-bar confirmation: +0.0838
- #12 continuation + two-bar confirmation + trend: +0.0994 (best)
- #13 added volatility regime filter: NOT SCORED (insufficient exposure)
- #14 strong close + one confirmation: -0.0245
- #15 session trend filter: +0.2145 (best)

#15 improved dramatically by adding a session-level trend filter. The key
insight is that outsized moves aligned with the session's dominant direction
continue more reliably. This variant builds on #15 but adds two refinements:

1. A minimum distance from the session open before taking signals. Early in
   the session, the session trend is not yet established, and outsized moves
   may be noise. We require at least `min_bars` bars to have passed since the
   session open before considering signals.

2. A stronger trend confirmation: instead of just checking that the current
   price is above/below the session open, we require that the outsized bar's
   close is at least `trend_strength` times the session's typical range away
   from the session open. This filters out weak trends where the price is
   barely above/below the open.

3. We also require that the outsized bar's close is in the direction of the
   move (close > open for longs, close < open for shorts), which was already
   in #15 but we keep it.

The hypothesis is that requiring a more established session trend and a
minimum number of bars before trading will filter out false signals that
occur early in the session or during weak trends, while preserving the strong
continuation signals that #15 identified.

Session state is reset on session boundaries, and all session-specific data
is kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar aligned with a strong session trend."""

    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 range_mult: float = 2.0, stop_mult: float = 1.0,
                 target_mult: float = 1.5, size: int = 1,
                 min_bars: int = 10, trend_strength: float = 0.5):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_bars = min_bars
        self.trend_strength = trend_strength
        self._session = None
        self._session_open_price: float | None = None
        self._ranges: deque[float] = deque(maxlen=lookback)
        self._pending_direction: int = 0  # 0=none, 1=long, -1=short
        self._pending_range: float = 0.0
        self._confirmation_count: int = 0
        self._prev_bar = None  # previous bar for trend comparison
        self._bars_this_session: int = 0

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
            self._bars_this_session = 0

        self._bars_this_session += 1

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
                            tag="outsized-strong-trend-confirmed-long")
                else:
                    ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                             tag="outsized-strong-trend-confirmed-short")
                # Clear pending signal.
                self._pending_direction = 0
                self._pending_range = 0.0
                self._confirmation_count = 0
                self._prev_bar = None
            return

        # No pending signal - check for outsized bar.
        if len(self._ranges) < self.lookback:
            return

        # Require minimum number of bars this session before trading.
        if self._bars_this_session < self.min_bars:
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
        price_move = bar.close - self._session_open_price
        session_trend = 1 if price_move > 0 else -1

        # Require trend strength: the move from open must be significant
        # relative to the session's typical range.
        if abs(price_move) < median * self.trend_strength:
            return

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
