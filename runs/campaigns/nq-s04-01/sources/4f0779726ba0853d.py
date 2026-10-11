"""Opening-range failure with a confirmed break and a retest entry.

Prior trials: #9 scored +0.0377, #11 scored -0.0201, #12 not scored, #13 not
scored, #14 identical to #13, #15 not scored, #16 not scored. The best fade
simply took the first break and let the bracket exit. This proposal keeps that
core but requires the break to be confirmed by a second bar closing beyond the
range, and enters on a retest of the broken boundary rather than immediately.

The failure account says a break of the opening range is more likely to fail
back into the range than to continue. A single bar closing beyond the range is
often noise; a second consecutive close beyond the range is stronger evidence
that the break is real. Entering on a retest of the broken boundary gives a
better price and filters out breaks that immediately continue.

The opening range is built from 5-minute bars whose close_ts falls between
08:30 and 09:00 Chicago. The break is a bar closing beyond the range. The
fade is taken only after two consecutive bars close beyond the range, and
then only when price retests the broken boundary from the outside.

The bracket is sized from the range's own width. The stop is placed at a
multiple of the range width beyond the entry, and the target is placed at a
multiple of the range width inside the range.

Chicago time uses a fixed -6 offset. The sample spans DST changes, but the
opening window itself is identified by the same offset consistently.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Fade a confirmed break of the opening range on a retest."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 0.5,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._last_session: int | None = None
        self._has_traded_this_session: bool = False
        self._break_side: str | None = None
        self._break_count: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _chicago_hour(self, ts_ns: int) -> datetime:
        dt_utc = datetime.fromtimestamp(ts_ns / NS_PER_SECOND, tz=timezone.utc)
        return dt_utc.astimezone(timezone(timedelta(seconds=self.chicago_offset)))

    def _in_opening_window(self, ts_ns: int) -> bool:
        local = self._chicago_hour(ts_ns)
        return local.hour == 8 and 30 <= local.minute < 60

    def on_bars(self, ctx) -> None:
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._has_traded_this_session = False
            self._break_side = None
            self._break_count = 0

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

        # Position management: let the bracket handle the exit.
        if ctx.position or ctx.working_order_ids:
            return

        # Only one fade per session.
        if self._has_traded_this_session:
            return

        rng = self._opening_high - self._opening_low
        if rng <= 0:
            return

        # Track consecutive closes beyond the range.
        if bar.close > self._opening_high:
            if self._break_side == "up":
                self._break_count += 1
            else:
                self._break_side = "up"
                self._break_count = 1
        elif bar.close < self._opening_low:
            if self._break_side == "down":
                self._break_count += 1
            else:
                self._break_side = "down"
                self._break_count = 1
        else:
            self._break_side = None
            self._break_count = 0

        # Require two consecutive closes beyond the range.
        if self._break_count < 2:
            return

        # Enter on a retest of the broken boundary from the outside.
        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if self._break_side == "up" and bar.low <= self._opening_high:
            # Price has come back to retest the broken high from above.
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-fade-short",
            )
            self._has_traded_this_session = True
        elif self._break_side == "down" and bar.high >= self._opening_low:
            # Price has come back to retest the broken low from below.
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
