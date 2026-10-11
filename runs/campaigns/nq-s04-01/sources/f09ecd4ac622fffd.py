"""Opening-range failure with a stronger break filter and time stop.

Prior trials: #9 scored +0.0377, #11 scored -0.0201, #12 not scored, #13 not
scored, #14 identical to #13. The best fade simply took the first break and
let the bracket exit. This proposal keeps that core but adds a stronger
volatility-scaled confirmation filter and a time stop.

The failure account says a break of the opening range is more likely to fail
back into the range than to continue. A fixed multiple of the range width
treats a quiet morning and a busy morning alike. Instead, the confirmation
threshold is scaled by the average true range of the opening window itself,
so a break must be larger on a volatile morning to be considered decisive.

The time stop closes the position after a fixed number of bars if the fade
does not work quickly. The idea is that the opening-range failure effect is
a morning phenomenon, and holding too long exposes the position to unrelated
moves.

The opening range is built from 5-minute bars whose close_ts falls between
08:30 and 09:00 Chicago. The break is a bar closing beyond the range. The
fade is taken only if the break bar's close is at least `min_break_atr_mult`
times the average true range beyond the range boundary.

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
        min_break_atr_mult: float = 0.75,
        max_hold_bars: int = 6,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_break_atr_mult = min_break_atr_mult
        self.max_hold_bars = max_hold_bars
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._opening_bars: list[tuple[float, float]] = []
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

    def _average_true_range(self) -> float:
        if len(self._opening_bars) < 2:
            return 0.0
        trs = []
        for i in range(1, len(self._opening_bars)):
            prev_high, prev_low = self._opening_bars[i - 1]
            high, low = self._opening_bars[i]
            tr = max(
                high - low,
                abs(high - prev_high),
                abs(low - prev_low),
            )
            trs.append(tr)
        return sum(trs) / len(trs)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._opening_bars = []
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
            self._opening_bars.append((bar.high, bar.low))
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

        atr = self._average_true_range()
        if atr <= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult
        min_break = atr * self.min_break_atr_mult

        if bar.close > self._opening_high + min_break:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-fade-short",
            )
            self._has_traded_this_session = True
            self._bars_since_entry = 0
        elif bar.close < self._opening_low - min_break:
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
