"""Opening-range breakout with a re-entry filter and a volatility-scaled stop.

The prior trials in this campaign tested continuation directly and scored
+0.0367, -0.1083, +0.0261, +0.0058, -0.0608. The simple continuation breakout
with a bracket (#2) remains the best. This proposal keeps the core claim but
adds two changes aimed at reducing the cost of false breaks without discarding
the signal.

First, a re-entry filter: after a breakout trade is closed (by stop, target, or
time), we do not immediately re-enter on the next bar that closes beyond the
range. The first break may be the day choosing a direction, but a second break
in the same direction shortly after a failed first is more likely to be noise
or a trap. We therefore require a cooldown of `reentry_cooldown_bars` bars after
a trade closes before allowing another entry.

Second, a volatility-scaled stop: instead of a fixed multiple of the opening
range, the stop is scaled by the ratio of the breakout bar's range to the
median bar range of the opening window. A high-conviction break (large bar
range relative to the opening bars) gets a wider stop, while a weak break gets
a tighter one. This adapts the risk to the strength of the signal and reduces
the chance of being stopped out by normal noise on a strong break.

The opening range is unchanged: the first 30 minutes after the 08:30 Chicago
open, built from 5-minute bars. A bar belongs to the opening window if its
close_ts falls between 08:30 and 09:00 Chicago. The break is a bar closing
beyond the range.

Chicago time uses a fixed -6 offset. The sample spans DST changes, but the
opening window itself is identified by the same offset consistently, so the
range construction is internally coherent.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Continuation breakout with a re-entry cooldown and volatility-scaled stop."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        reentry_cooldown_bars: int = 3,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.reentry_cooldown_bars = reentry_cooldown_bars
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._opening_ranges: list[float] = []
        self._last_session: int | None = None
        self._bars_since_trade_close: int = 0
        self._had_trade_this_session: bool = False

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
        self._opening_ranges = []
        self._bars_since_trade_close = 0
        self._had_trade_this_session = False

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
            self._opening_ranges.append(bar.high - bar.low)
            return

        # We are past the opening window.
        if self._opening_high is None or self._opening_low is None:
            return

        # Track whether we have a trade open and manage the cooldown.
        if ctx.position or ctx.working_order_ids:
            # A trade is open; reset the cooldown counter.
            self._bars_since_trade_close = 0
            return

        # No position. Increment cooldown counter if we have traded this session.
        if self._had_trade_this_session:
            self._bars_since_trade_close += 1
            if self._bars_since_trade_close < self.reentry_cooldown_bars:
                return

        rng = self._opening_high - self._opening_low
        if rng <= 0:
            return

        # Volatility scaling for the stop.
        if not self._opening_ranges:
            return
        sorted_ranges = sorted(self._opening_ranges)
        median_bar_range = sorted_ranges[len(sorted_ranges) // 2]
        if median_bar_range <= 0:
            return

        bar_range = bar.high - bar.low
        vol_scale = max(0.5, min(2.0, bar_range / median_bar_range))

        stop = rng * self.stop_mult * vol_scale
        target = rng * self.target_mult

        if bar.close > self._opening_high:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-breakout-long",
            )
            self._had_trade_this_session = True
            self._bars_since_trade_close = 0
        elif bar.close < self._opening_low:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-breakout-short",
            )
            self._had_trade_this_session = True
            self._bars_since_trade_close = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
