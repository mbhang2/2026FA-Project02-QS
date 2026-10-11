"""Opening-range breakout with a time-of-day filter and a smaller, faster target.

Builds on the prior strategy, which traded any close beyond the opening range
and scored +0.0367. Two changes are made to test whether the effect is
strongest in the first hour after the range forms:

1. Only take a breakout if it occurs before 10:00 Chicago. The claim is that
   the repricing of overnight information is concentrated in the first 90
   minutes; later breaks are more likely to be noise or mean reversion.
2. Use a tighter target multiplier (1.0 instead of 1.5) and a tighter stop
   multiplier (0.35 instead of 0.5). The prior strategy's wider target may
   have given back gains on failed continuations; a faster exit captures the
   initial impulse while the stop still limits the loss.

The opening range itself is unchanged: the first 30 minutes after the 08:30
Chicago open, built from 5-minute bars. A break is a bar closing beyond the
range, and we enter in the direction of the break.

Chicago time conversion uses a fixed offset with no DST handling, which is a
known simplification. The sample spans DST changes, but the error is at most
one hour and only affects the time-of-day cutoff, not the opening range
construction (which uses the same offset consistently for the 08:30-09:00
window). The offset is -6 hours for CST; during CDT the actual offset is -5,
so the cutoff fires one hour late in local terms. This is acceptable for a
first test of the time filter.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade an opening-range breakout only in the first 90 minutes, with a tight bracket."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 0.35,
        target_mult: float = 1.0,
        chicago_offset: int = -6 * 3600,
        cutoff_hour: int = 10,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.chicago_offset = chicago_offset
        self.cutoff_hour = cutoff_hour
        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _chicago_hour(self, ts_ns: int) -> datetime:
        dt_utc = datetime.fromtimestamp(ts_ns / NS_PER_SECOND, tz=timezone.utc)
        return dt_utc.astimezone(timezone(timedelta(seconds=self.chicago_offset)))

    def _in_opening_window(self, ts_ns: int) -> bool:
        local = self._chicago_hour(ts_ns)
        return local.hour == 8 and 30 <= local.minute < 60

    def _before_cutoff(self, ts_ns: int) -> bool:
        local = self._chicago_hour(ts_ns)
        return local.hour < self.cutoff_hour

    def on_bars(self, ctx) -> None:
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self._opening_high = None
            self._opening_low = None

        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if not history:
            return

        bar = history[-1]

        if self._in_opening_window(bar.close_ts):
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            return

        if self._opening_high is None or self._opening_low is None:
            return
        if not self._before_cutoff(bar.close_ts):
            return
        if ctx.position or ctx.working_order_ids:
            return

        rng = self._opening_high - self._opening_low
        if rng <= 0:
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
