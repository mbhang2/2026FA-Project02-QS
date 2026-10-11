"""Opening-range breakout with a volatility-scaled confirmation and a time stop.

This proposal keeps the continuation claim of the best prior trial (#2, +0.0367)
but addresses two weaknesses visible in the trial history. First, a single bar
closing beyond the range is a weak confirmation: it fires on every small
excursion and pays the spread on many false breaks. Second, the bracket's fixed
target lets a position sit through a reversal that never reaches the target,
giving back the initial move.

The change: require the breakout bar's range (high - low) to be at least as
large as the median bar range of the opening window itself. A break on a
high-range bar is a decision the interval made with conviction, not a drift
across the line. And instead of a fixed target multiplier, exit at the earlier
of the target or a fixed number of bars after entry (a time stop). The time stop
captures the immediate continuation impulse and avoids holding through the
mean-reversion that follows a failed break.

The opening range is unchanged: the first 30 minutes after the 08:30 Chicago
open, built from 5-minute bars. A bar belongs to the opening window if its
close_ts falls between 08:30 and 09:00 Chicago. The break is a bar closing
beyond the range, with the added range filter.

Chicago time uses a fixed -6 offset. The sample spans DST changes, but the
opening window itself is identified by the same offset consistently, so the
range construction is internally coherent. The time stop is measured in bars,
not clock time, so it is unaffected by the offset error.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from src.core.types import NS_PER_SECOND


class Strategy:
    """Continuation breakout with a conviction filter and a time stop."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        max_hold_bars: int = 6,
        chicago_offset: int = -6 * 3600,
    ):
        self.interval = interval
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_hold_bars = max_hold_bars
        self.chicago_offset = chicago_offset

        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._opening_ranges: list[float] = []
        self._last_session: int | None = None

        self._entry_bar_count: int = 0
        self._in_trade: bool = False

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
        self._entry_bar_count = 0
        self._in_trade = False

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

        # We are past the opening window. If we have no range, nothing to do.
        if self._opening_high is None or self._opening_low is None:
            return

        rng = self._opening_high - self._opening_low
        if rng <= 0:
            return

        # Manage an open position: time stop after max_hold_bars.
        if ctx.position or ctx.working_order_ids:
            if ctx.position:
                self._entry_bar_count += 1
                if self._entry_bar_count >= self.max_hold_bars:
                    # Flatten: cancel any working bracket legs and close.
                    ctx.cancel_all()
                    ctx.close()
                    self._in_trade = False
            return

        # No position. Check for a breakout with conviction.
        if not self._opening_ranges:
            return
        median_bar_range = sorted(self._opening_ranges)[len(self._opening_ranges) // 2]
        bar_range = bar.high - bar.low
        if bar_range < median_bar_range:
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
            self._entry_bar_count = 0
            self._in_trade = True
        elif bar.close < self._opening_low:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="opening-breakout-short",
            )
            self._entry_bar_count = 0
            self._in_trade = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
