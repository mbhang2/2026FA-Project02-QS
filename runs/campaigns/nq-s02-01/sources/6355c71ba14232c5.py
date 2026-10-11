"""Investigate whether an outsized move inside a session continues or gives back.

Prior results:
- #11 continuation + one-bar confirmation: +0.0838
- #12 continuation + two-bar confirmation + trend: +0.0994 (best)

#12 improved on #11 by requiring two consecutive confirmation bars with
trend characteristics. This variant builds on #12 by adding a volatility
regime filter that was dropped in #11 but showed promise in #6 (+0.0166).

Key changes from #12:
1. Add a volatility regime filter: only take trades when the session's
   recent volatility is elevated relative to its own history. This was
   the key filter in #6 that helped, but #11 and #12 dropped it.
2. Keep the two-bar confirmation with trend from #12, which was the
   strongest signal.
3. Use a relative volatility measure: compare the current median range
   to a longer-term median of session ranges to determine if we are in
   a high-volatility regime.

The hypothesis is that outsized moves are more likely to continue when
the overall session volatility is elevated, as this indicates genuine
information flow rather than a one-off spike.

Session state is reset on session boundaries, and all session-specific data
is kept on self so the bounded window is never a problem.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade continuation after an outsized bar in high-volatility regimes."""

    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 vol_lookback: int = 60, range_mult: float = 2.0,
                 vol_mult: float = 1.2, stop_mult: float = 1.0,
                 target_mult: float = 1.5, size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.vol_lookback = vol_lookback
        self.range_mult = range_mult
        self.vol_mult = vol_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._ranges: deque[float] = deque(maxlen=lookback)
        self._vol_ranges: deque[float] = deque(maxlen=vol_lookback)
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
            self._vol_ranges.clear()
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
        self._vol_ranges.append(rng)

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
                            tag="outsized-vol-confirmed-long")
                else:
                    ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                             tag="outsized-vol-confirmed-short")
                # Clear pending signal.
                self._pending_direction = 0
                self._pending_range = 0.0
                self._confirmation_count = 0
                self._prev_bar = None
            return

        # No pending signal - check for outsized bar.
        if len(self._ranges) < self.lookback or len(self._vol_ranges) < self.vol_lookback:
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

        # Volatility regime: compare current median to longer-term median.
        vol_ordered = sorted(self._vol_ranges)
        vol_mid = len(vol_ordered) // 2
        if len(vol_ordered) % 2 == 0:
            vol_median = (vol_ordered[vol_mid - 1] + vol_ordered[vol_mid]) / 2.0
        else:
            vol_median = float(vol_ordered[vol_mid])
        if vol_median <= 0:
            return

        # Only trade in elevated volatility regimes.
        if median < vol_median * self.vol_mult:
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
