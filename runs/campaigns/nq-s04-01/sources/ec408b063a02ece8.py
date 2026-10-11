"""Opening-range breakout with a failure filter: fade the first break, take the second.

The prior strategies tested continuation directly and scored +0.0367 and -0.1083.
This proposal tests the opposite account: the first break of the opening range is
often a failure, a stop run that exhausts the early flow. We fade that first break
with a small, quick target, and only take a continuation trade if price returns
inside the range and then breaks out again in the same direction.

The opening range is the first 30 minutes after the 08:30 Chicago open, built from
5-minute bars. A break is a bar closing beyond the range. The first break in a
session is faded: we sell a close above the range and buy a close below it, with a
tight bracket sized from the range width. If that fade is stopped out, the
stop-out itself is information: the break was real. We then wait for price to
re-enter the range and break out again in the same direction, and take that second
break as a continuation trade.

Chicago time is handled with a fixed offset; the sample spans DST changes but the
error is at most one hour and affects only the time-of-day logic, not the range
construction. Outside the opening window and the two trade setups we do nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Fade the first opening-range break, then trade the second break as continuation."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        fade_stop_mult: float = 0.5,
        fade_target_mult: float = 0.5,
        cont_stop_mult: float = 0.5,
        cont_target_mult: float = 1.5,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.fade_stop_mult = fade_stop_mult
        self.fade_target_mult = fade_target_mult
        self.cont_stop_mult = cont_stop_mult
        self.cont_target_mult = cont_target_mult
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._last_session: int | None = None

        # State for the two-phase logic
        self._first_break_direction: int | None = None  # +1 long, -1 short
        self._fade_entered: bool = False
        self._fade_stopped_out: bool = False
        self._reentered_range: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _chicago_hour(self, ts_ns: int) -> datetime:
        dt_utc = datetime.fromtimestamp(ts_ns / NS_PER_SECOND, tz=timezone.utc)
        return dt_utc.astimezone(timezone(timedelta(seconds=self.chicago_offset)))

    def _in_opening_window(self, ts_ns: int) -> bool:
        local = self._chicago_hour(ts_ns)
        return local.hour == 8 and 30 <= local.minute < 60

    def _reset_session_state(self) -> None:
        self._opening_high = None
        self._opening_low = None
        self._first_break_direction = None
        self._fade_entered = False
        self._fade_stopped_out = False
        self._reentered_range = False

    def on_bars(self, ctx) -> None:
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self._reset_session_state()

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

        if self._opening_high is None or self._opening_low is None:
            return

        rng = self._opening_high - self._opening_low
        if rng <= 0:
            return

        # Determine if this bar is a break and in which direction
        broke_high = bar.close > self._opening_high
        broke_low = bar.close < self._opening_low

        # If we have no position and no working orders, we can consider a new trade
        if ctx.position or ctx.working_order_ids:
            # We are in a trade. Check if it was the fade and it got stopped out.
            # We detect stop-out by checking if our fade position was closed and
            # the first break direction is still set.
            if self._fade_entered and not ctx.position and not ctx.working_order_ids:
                # The fade position was closed. If it hit the stop, the break was real.
                # We assume a stop-out if price is still beyond the range in the
                # direction of the first break.
                if self._first_break_direction == 1 and ctx.last_price > self._opening_high:
                    self._fade_stopped_out = True
                elif self._first_break_direction == -1 and ctx.last_price < self._opening_low:
                    self._fade_stopped_out = True
                self._fade_entered = False
            return

        # No position, no working orders
        if self._first_break_direction is None:
            # This is the first break of the session. Fade it.
            if broke_high:
                self._first_break_direction = 1
                self._fade_entered = True
                ctx.sell(
                    size=self.size,
                    stop_offset=rng * self.fade_stop_mult,
                    target_offset=rng * self.fade_target_mult,
                    tag="fade-first-break",
                )
            elif broke_low:
                self._first_break_direction = -1
                self._fade_entered = True
                ctx.buy(
                    size=self.size,
                    stop_offset=rng * self.fade_stop_mult,
                    target_offset=rng * self.fade_target_mult,
                    tag="fade-first-break",
                )
            return

        # We have already seen the first break. Check for re-entry into the range.
        if not self._reentered_range:
            if self._opening_low <= bar.close <= self._opening_high:
                self._reentered_range = True
            return

        # We have re-entered the range. Look for a second break in the same direction
        # as the first break, but only if the fade was stopped out (break was real).
        if not self._fade_stopped_out:
            return

        if self._first_break_direction == 1 and broke_high:
            ctx.buy(
                size=self.size,
                stop_offset=rng * self.cont_stop_mult,
                target_offset=rng * self.cont_target_mult,
                tag="second-break-continuation",
            )
        elif self._first_break_direction == -1 and broke_low:
            ctx.sell(
                size=self.size,
                stop_offset=rng * self.cont_stop_mult,
                target_offset=rng * self.cont_target_mult,
                tag="second-break-continuation",
            )

    def on_fill(self, ctx) -> None:
        # If a fade position was filled and then stopped out, the engine will
        # have closed it. We detect that in on_bars by checking position status.
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
