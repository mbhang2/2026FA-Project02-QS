"""Opening-range breakout with a time-decay filter and a volatility-scaled target.

The prior trials in this campaign tested continuation directly and scored
+0.0367, -0.1083, +0.0261, +0.0058. The best result came from a simple
continuation breakout with a bracket. This proposal keeps the core claim but
adds two changes aimed at reducing the cost of false breaks.

First, a time-decay filter: we only take a breakout if it occurs within a
limited number of bars after the opening window closes. The first 30 minutes
establish the range; the information in a break decays as the session wears on.
A break at 11:00 is less likely to be the day choosing a direction than a break
at 09:05. We therefore require the breakout bar to close within `max_breakout_delay`
bars after the opening window.

Second, a volatility-scaled target: instead of a fixed multiple of the opening
range, the target is scaled by the ratio of the breakout bar's range to the
median bar range of the opening window. A high-conviction break (large bar
range relative to the opening bars) gets a larger target, while a weak break
gets a smaller one. This adapts the profit objective to the strength of the
signal.

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
    """Continuation breakout with time decay and volatility-scaled target."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        max_breakout_delay: int = 6,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_breakout_delay = max_breakout_delay
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._opening_ranges: list[float] = []
        self._opening_window_bars: int = 0
        self._bars_since_opening: int = 0
        self._last_session: int | None = None

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
        self._opening_window_bars = 0
        self._bars_since_opening = 0

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
            self._opening_window_bars += 1
            return

        # We are past the opening window. Count bars since the window closed.
        if self._opening_high is None or self._opening_low is None:
            return

        self._bars_since_opening += 1

        # Time decay: only act within a limited number of bars after the opening.
        if self._bars_since_opening > self.max_breakout_delay:
            return

        if ctx.position or ctx.working_order_ids:
            return

        rng = self._opening_high - self._opening_low
        if rng <= 0:
            return

        # Volatility scaling: compare the breakout bar's range to the median
        # opening-window bar range.
        if not self._opening_ranges:
            return
        sorted_ranges = sorted(self._opening_ranges)
        median_bar_range = sorted_ranges[len(sorted_ranges) // 2]
        if median_bar_range <= 0:
            return

        bar_range = bar.high - bar.low
        vol_scale = max(0.5, min(2.0, bar_range / median_bar_range))

        stop = rng * self.stop_mult
        target = rng * self.target_mult * vol_scale

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
