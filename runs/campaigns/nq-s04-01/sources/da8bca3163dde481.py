"""Opening-range failure with a volatility-scaled confirmation filter.

The prior fade (#9) scored +0.0377, and the confirmation/time-stop variant
(#10) was not scored because it was exposed in only 36 of 384 sessions, under
the floor. This proposal keeps the fade but replaces the fixed minimum break
multiple with a volatility-scaled threshold and removes the time-stop.

The failure account says a break of the opening range is more likely to fail
back into the range than to continue. A fixed multiple of the range width
treats a quiet morning and a busy morning alike. Instead, the confirmation
threshold is scaled by the average true range of the opening window itself,
so a break must be larger on a volatile morning to be considered decisive.

The opening range is built from 5-minute bars whose close_ts falls between
08:30 and 09:00 Chicago. The break is a bar closing beyond the range. The
fade is taken only if the break bar's close is at least `min_break_atr_mult`
times the average true range beyond the range boundary.

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
    """Fade the first decisive break of the opening range, volatility-scaled."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 0.5,
        min_break_atr_mult: float = 0.5,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_break_atr_mult = min_break_atr_mult
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._opening_bars: list[tuple[float, float]] = []
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
        if ctx.position or ctx.working_order_ids:
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
        elif bar.close < self._opening_low - min_break:
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
