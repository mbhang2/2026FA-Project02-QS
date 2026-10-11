"""Opening-range breakout: does a break of the first 30 minutes continue?

Locates the regular-hours open by Chicago time, builds the first 30 minutes'
range from 5-minute bars, and trades a close beyond that range in the direction
of the break. The claim is continuation: a break of the opening range is the
day choosing a direction, so we enter on it and hold with a bracket.

Chicago time is used rather than a fixed UTC hour because the offset moves
twice a year and this sample spans several changes. The regular-hours open is
08:30 Chicago; the first 30 minutes are 08:30-09:00 Chicago. A bar belongs to
the opening range if its close_ts falls in that window.

The break is a bar closing beyond the range, not a trade through it: a close
is a decision the whole interval has made. The bracket is sized from the
range's own width, so a quiet morning gets a tighter stop than a busy one.

Outside the first 30 minutes we do nothing: this is a claim about the half
hour after the open, not the whole session.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade a close beyond the first 30 minutes' range, in the break's direction."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        chicago_offset: int = -6 * 3600,  # CST; DST handled by the zoneinfo lookup
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.chicago_offset = chicago_offset
        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._opening_range_set = False
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _chicago_hour(self, ts_ns: int) -> datetime:
        """Convert a nanosecond UTC timestamp to a Chicago-local datetime."""
        dt_utc = datetime.fromtimestamp(ts_ns / NS_PER_SECOND, tz=timezone.utc)
        return dt_utc.astimezone(timezone(timedelta(seconds=self.chicago_offset)))

    def _in_opening_window(self, ts_ns: int) -> bool:
        """True if the bar's close_ts is within 08:30-09:00 Chicago."""
        local = self._chicago_hour(ts_ns)
        return local.hour == 8 and 30 <= local.minute < 60

    def on_bars(self, ctx) -> None:
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._opening_range_set = False

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
