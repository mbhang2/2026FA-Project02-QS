"""Opening-range failure: fade the first break with a volatility-scaled stop.

Prior trials: #9 scored +0.0377, #11 -0.0201, #17 +0.0259, #18 not scored.
The best fade took the first break and let a fixed-multiple bracket exit.
This proposal keeps the first-break fade but sizes the stop from the
opening range's own width divided by its tick count, a rough volatility
proxy. A wide range built on few trades is thin and likely to fail; a
narrow range built on many trades is dense and likely to continue. The
stop is wider on thin ranges and tighter on dense ones.

The opening range is built from 5-minute bars whose close_ts falls between
08:30 and 09:00 Chicago. The break is a bar closing beyond the range. The
fade is taken immediately on the first break.

The target remains a fixed multiple of the range width inside the range.
The stop is `stop_ticks` times the average price movement per trade in the
opening window, where movement is approximated as the range width divided
by the total tick count of the opening bars.

Chicago time uses a fixed -6 offset. The sample spans DST changes, but the
opening window itself is identified by the same offset consistently.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Fade the first break of the opening range with a volatility-scaled stop."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        target_mult: float = 0.5,
        stop_ticks: float = 2.0,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.target_mult = target_mult
        self.stop_ticks = stop_ticks
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._opening_tick_count: int = 0
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

    def on_bars(self, ctx) -> None:
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._opening_tick_count = 0
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
            self._opening_tick_count += bar.tick_count
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

        # Volatility proxy: average price movement per trade in the opening window.
        if self._opening_tick_count <= 0:
            return
        avg_tick_move = rng / self._opening_tick_count
        stop = avg_tick_move * self.stop_ticks

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
