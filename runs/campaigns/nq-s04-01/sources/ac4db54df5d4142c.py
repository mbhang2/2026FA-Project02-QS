"""Opening-range breakout with a volatility filter and time-of-day exit.

Prior trials: simple continuation scored +0.0367; time-decay and re-entry
filters both underperformed. This proposal keeps the core continuation claim
but adds two changes aimed at reducing false-break costs without discarding
the signal.

First, a volatility filter: the breakout is only taken if the opening range
is large enough relative to the median bar range of the opening window. A
narrow range on a quiet morning is more likely to be noise; a wide range on
an active morning is more likely to be a meaningful day. We require the
opening range to be at least `min_range_mult` times the median opening bar
range.

Second, a time-of-day exit: positions are closed at a fixed Chicago time
(15:00, the cash-equity close) rather than held until the session's Globex
close. The claim is about the half hour after the open; carrying a position
into the afternoon adds exposure to a different regime. We exit flat at
15:00 Chicago if still in a position.

The opening range is unchanged: the first 30 minutes after the 08:30 Chicago
open, built from 5-minute bars. A bar belongs to the opening window if its
close_ts falls between 08:30 and 09:00 Chicago. The break is a bar closing
beyond the range.

Chicago time uses a fixed -6 offset. The sample spans DST changes, but the
opening window itself is identified by the same offset consistently.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Continuation breakout with volatility filter and time-of-day exit."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        min_range_mult: float = 1.0,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range_mult = min_range_mult
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._opening_ranges: list[float] = []
        self._last_session: int | None = None
        self._exit_time_ns: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _chicago_hour(self, ts_ns: int) -> datetime:
        dt_utc = datetime.fromtimestamp(ts_ns / NS_PER_SECOND, tz=timezone.utc)
        return dt_utc.astimezone(timezone(timedelta(seconds=self.chicago_offset)))

    def _in_opening_window(self, ts_ns: int) -> bool:
        local = self._chicago_hour(ts_ns)
        return local.hour == 8 and 30 <= local.minute < 60

    def _compute_exit_time_ns(self, session_start_ns: int) -> int:
        """Compute the 15:00 Chicago exit time for the current session."""
        session_start_local = self._chicago_hour(session_start_ns)
        exit_local = session_start_local.replace(hour=15, minute=0, second=0, microsecond=0)
        exit_utc = exit_local.astimezone(timezone.utc)
        return int(exit_utc.timestamp() * NS_PER_SECOND)

    def _reset_session_state(self, session_start_ns: int) -> None:
        self._opening_high = None
        self._opening_low = None
        self._opening_ranges = []
        self._exit_time_ns = self._compute_exit_time_ns(session_start_ns)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self._reset_session_state(ctx.session_start)

        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if not history:
            return

        bar = history[-1]

        # Time-of-day exit: if we have a position and it's past 15:00 Chicago,
        # close it.
        if self._exit_time_ns is not None and ctx.ts >= self._exit_time_ns:
            if ctx.position:
                ctx.close()
                return
            if ctx.working_order_ids:
                ctx.cancel_all()
                return

        # Build the opening range from bars that closed in the first 30 minutes.
        if self._in_opening_window(bar.close_ts):
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            self._opening_ranges.append(bar.high - bar.low)
            return

        # Only act once we have a range and are past the opening window.
        if self._opening_high is None or self._opening_low is None:
            return
        if ctx.position or ctx.working_order_ids:
            return

        rng = self._opening_high - self._opening_low
        if rng <= 0:
            return

        # Volatility filter: require the opening range to be large enough.
        if not self._opening_ranges:
            return
        sorted_ranges = sorted(self._opening_ranges)
        median_bar_range = sorted_ranges[len(sorted_ranges) // 2]
        if median_bar_range <= 0:
            return
        if rng < self.min_range_mult * median_bar_range:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > self._opening_high:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-breakout-long",
            )
        elif bar.close < self._opening_low:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-breakout-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
