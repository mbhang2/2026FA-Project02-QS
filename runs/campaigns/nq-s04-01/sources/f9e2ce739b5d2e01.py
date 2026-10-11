"""Opening-range failure with a time-of-day exit and a wider stop.

The prior fade (#9) scored +0.0377, and #11 scored -0.0201. The best fade
simply took the first break and let the bracket exit. This proposal keeps that
core but changes the exit logic.

The failure account says a break of the opening range should fail back into
the range quickly. If it does not, the position is likely wrong. Instead of
relying only on the bracket, we add a time-of-day exit: if the fade is still
open at 11:00 Chicago, we close it. The idea is that the opening-range failure
effect is a morning phenomenon, and holding into the afternoon exposes the
position to unrelated moves.

We also widen the stop to 1.5 times the range width, giving the fade more room
to work before being stopped out, while keeping the target at 0.5 times the
range width inside the range.

The opening range is built from 5-minute bars whose close_ts falls between
08:30 and 09:00 Chicago. The break is a bar closing beyond the range.

Chicago time uses a fixed -6 offset. The sample spans DST changes, but the
opening window itself is identified by the same offset consistently.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Fade the first break of the opening range, with a time-of-day exit."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 1.5,
        target_mult: float = 0.5,
        exit_hour: int = 11,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.exit_hour = exit_hour
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._last_session: int | None = None
        self._has_traded_this_session: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _chicago_hour(self, ts_ns: int) -> datetime:
        dt_utc = datetime.fromtimestamp(ts_ns / NS_PER_SECOND, tz=timezone.utc)
        return dt_utc.astimezone(timezone(timedelta(seconds=self.chicago_offset)))

    def _in_opening_window(self, ts_ns: int) -> bool:
        local = self._chicago_hour(ts_ns)
        return local.hour == 8 and 30 <= local.minute < 60

    def _past_exit_time(self, ts_ns: int) -> bool:
        local = self._chicago_hour(ts_ns)
        return local.hour >= self.exit_hour

    def on_bars(self, ctx) -> None:
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._has_traded_this_session = False

        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if not history:
            return

        bar = history[-1]

        # Build the opening range from bars that closed in the first 30 minutes.
        if self._in_opening_window(bar.close_ts):
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            return

        # Only act once we have a range and are past the opening window.
        if self._opening_high is None or self._opening_low is None:
            return

        # Time-of-day exit: close any open position after exit_hour Chicago.
        if ctx.position and self._past_exit_time(bar.close_ts):
            ctx.close()
            return

        if ctx.position or ctx.working_order_ids:
            return

        # Only one fade per session.
        if self._has_traded_this_session:
            return

        rng = self._opening_high - self._opening_low
        if rng <= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > self._opening_high:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-fade-short",
            )
            self._has_traded_this_session = True
        elif bar.close < self._opening_low:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-fade-long",
            )
            self._has_traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
