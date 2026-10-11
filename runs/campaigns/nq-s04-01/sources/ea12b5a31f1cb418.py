"""Opening-range failure with a measured breakout and a time stop, revised.

Prior trials: #9 scored +0.0377, #11 scored -0.0201, #12 not scored, #13 not
scored, #14 identical to #13, #15 not scored, #16 not scored, #17 scored
+0.0259. The best fade simply took the first break and let the bracket exit.
This proposal keeps that core but adds a measured breakout filter based on the
opening range's own width and a time stop.

The failure account says a break of the opening range is more likely to fail
back into the range than to continue. A fixed number of points treats a quiet
morning and a busy morning alike. Instead, the breakout threshold is scaled
by the range width itself, so a break must be larger on a wide morning to be
considered decisive.

We also add a time stop: if the fade is still open after a fixed number of
bars past the entry, we close it. The idea is that the opening-range failure
effect is a morning phenomenon, and holding too long exposes the position to
unrelated moves.

The opening range is built from 5-minute bars whose close_ts falls between
08:30 and 09:00 Chicago. The break is a bar closing beyond the range. The
fade is taken only if the break bar's close is at least `breakout_width_mult`
times the range width beyond the range boundary.

The bracket is sized from the range's own width. The stop is placed at a
multiple of the range width beyond the entry, and the target is placed at a
multiple of the range width inside the range.

Chicago time uses a fixed -6 offset. The sample spans DST changes, but the
opening window itself is identified by the same offset consistently.

The time stop is implemented by tracking the number of bars since entry on
self. When the count reaches `max_hold_bars`, the position is closed.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Fade the first decisive break of the opening range, with a time stop."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 0.5,
        breakout_width_mult: float = 0.25,
        max_hold_bars: int = 6,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.breakout_width_mult = breakout_width_mult
        self.max_hold_bars = max_hold_bars
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._last_session: int | None = None
        self._has_traded_this_session: bool = False
        self._bars_since_entry: int = 0

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
            self._bars_since_entry = 0

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

        # Time stop: close any open position after max_hold_bars.
        if ctx.position:
            self._bars_since_entry += 1
            if self._bars_since_entry >= self.max_hold_bars:
                ctx.close()
                self._bars_since_entry = 0
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
        breakout = rng * self.breakout_width_mult

        if bar.close > self._opening_high + breakout:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-fade-short",
            )
            self._has_traded_this_session = True
            self._bars_since_entry = 0
        elif bar.close < self._opening_low - breakout:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-fade-long",
            )
            self._has_traded_this_session = True
            self._bars_since_entry = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
