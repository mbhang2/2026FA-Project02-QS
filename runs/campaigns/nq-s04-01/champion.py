"""Opening-range failure: fade the first break, not follow it.

The prior trials in this campaign tested continuation and scored +0.0367,
-0.1083, +0.0261, +0.0058, -0.0608, +0.0348, -0.0107. The simple continuation
breakout (#2) remains the best, but the scores are small and mixed. This
proposal tests the opposite account: a break of the opening range is more
likely to fail back into the range than to continue.

The first 30 minutes after the 08:30 Chicago open are thin and full of orders
that had to be done at the open regardless of price. A move beyond that range
is as likely to be that flow exhausting itself as it is to be the day choosing
a direction. We therefore take the opposite side of the break: when a bar
closes above the opening high, we sell short; when a bar closes below the
opening low, we buy. The position is closed when price returns inside the
range, or by a bracket.

The opening range is built from 5-minute bars whose close_ts falls between
08:30 and 09:00 Chicago. A bar belongs to the opening window if its close_ts
falls in that window. The break is a bar closing beyond the range.

The bracket is sized from the range's own width. The stop is placed at a
multiple of the range width beyond the entry, and the target is placed at a
multiple of the range width inside the range, so a successful fade returns
price to the range and takes profit.

Chicago time uses a fixed -6 offset. The sample spans DST changes, but the
opening window itself is identified by the same offset consistently.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Fade the first break of the opening range."""

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
