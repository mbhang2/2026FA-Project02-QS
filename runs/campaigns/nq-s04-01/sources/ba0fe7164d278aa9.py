"""Opening-range failure with a confirmation filter and time-stop.

The prior fade (#9) scored +0.0377, slightly better than the best continuation
(#2, +0.0367). This proposal keeps the fade but adds two changes aimed at
filtering false breaks and limiting exposure.

First, a confirmation filter: the fade is only taken if the break bar's close
is at least `min_break_mult` times the opening range beyond the range boundary.
A marginal break of a few ticks is more likely to be noise; a decisive break
that closes well beyond the range is more likely to exhaust itself.

Second, a time-stop: if the fade does not reach its target within
`max_hold_bars` bars, the position is closed. The failure account says the
break should fail back into the range quickly; if it does not, the
continuation account is more likely to be right and we should not hold the
position.

The opening range is unchanged: the first 30 minutes after the 08:30 Chicago
open, built from 5-minute bars whose close_ts falls between 08:30 and 09:00
Chicago. The break is a bar closing beyond the range.

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
    """Fade the first decisive break of the opening range, with a time-stop."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 0.5,
        min_break_mult: float = 0.25,
        max_hold_bars: int = 6,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_break_mult = min_break_mult
        self.max_hold_bars = max_hold_bars
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._last_session: int | None = None
        self._has_traded_this_session: bool = False
        self._bars_in_position: int = 0

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
            self._bars_in_position = 0

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

        # Time-stop: if we are in a position and have held it too long, close.
        if ctx.position:
            self._bars_in_position += 1
            if self._bars_in_position >= self.max_hold_bars:
                ctx.close()
                return
            return

        if ctx.working_order_ids:
            return

        # Only one fade per session.
        if self._has_traded_this_session:
            return

        rng = self._opening_high - self._opening_low
        if rng <= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult
        min_break = rng * self.min_break_mult

        if bar.close > self._opening_high + min_break:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-fade-short",
            )
            self._has_traded_this_session = True
            self._bars_in_position = 0
        elif bar.close < self._opening_low - min_break:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-fade-long",
            )
            self._has_traded_this_session = True
            self._bars_in_position = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
