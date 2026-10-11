"""Opening-range breakout: does a break of the first 30 minutes continue or fail?

This strategy investigates the campaign question with a conditional approach
based on the opening range's width relative to recent sessions.

The claim: when the opening range is unusually wide relative to recent sessions,
it represents genuine repricing rather than thin forced flow, and a break of it
is more likely to continue than to fail. On such days we trade WITH the break
(momentum), not against it. On ordinary days we stay flat.

Time convention: Chicago offset is derived per-session from the date using the
standard US DST rule. RTH open is 08:30 Chicago, close is 15:00 Chicago.
The opening range is the first 30 minutes: 08:30 to 09:00 Chicago.

Range width scaling: we maintain a rolling median of opening-range widths from
the previous 20 sessions. A session qualifies as "strong" if its opening range
width exceeds 1.25x that median. This is known at the moment the range completes,
before any break is traded.

Break definition: the first bar after the range whose close is outside the range.
Entry is at that bar's close, with a bracket sized from the range width.
Stop is 0.75x the range width, target is 1.5x the range width, giving the trade
room to develop while still capturing the continuation.

Exits: bracket plus a time stop. If the position is still open after 8 bars
(40 minutes), we flatten it with a market order. This prevents the position from
drifting into hours the claim does not cover. We do not trade in the last hour
of RTH.

Weekday and month splits are reported by the run record; the strategy itself
does not condition on them.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone, timedelta


class Strategy:
    """Continuation on strong opening ranges, flat otherwise."""

    def __init__(self, *, interval: str = "5m", range_minutes: int = 30,
                 stop_mult: float = 0.75, target_mult: float = 1.5,
                 size: int = 1, median_lookback: int = 20,
                 strength_mult: float = 1.25, max_hold_bars: int = 8):
        self.interval = interval
        self.range_minutes = range_minutes
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.median_lookback = median_lookback
        self.strength_mult = strength_mult
        self.max_hold_bars = max_hold_bars

        # Session state
        self._session: int | None = None
        self._open_ts: int = 0
        self._range_close_ts: int = 0
        self._range_high: float | None = None
        self._range_low: float | None = None
        self._range_done: bool = False
        self._trade_taken: bool = False
        self._is_strong: bool = False
        self._entry_bar_count: int = 0

        # Rolling history of range widths
        self._range_widths: deque = deque(maxlen=median_lookback)

    @staticmethod
    def _chicago_offset_ns(session_start_ns: int) -> int:
        dt = datetime.fromtimestamp(session_start_ns / 1e9, tz=timezone.utc)
        year = dt.year
        march_first = datetime(year, 3, 1, tzinfo=timezone.utc)
        march_second_sunday = march_first + timedelta(
            days=(6 - march_first.weekday() + 7) % 7 + 7
        )
        nov_first = datetime(year, 11, 1, tzinfo=timezone.utc)
        nov_first_sunday = nov_first + timedelta(
            days=(6 - nov_first.weekday()) % 7
        )
        if march_second_sunday <= dt < nov_first_sunday:
            return -5 * 3600 * 1_000_000_000
        return -6 * 3600 * 1_000_000_000

    def _rth_open_ns(self, session_start_ns: int) -> int:
        offset_ns = self._chicago_offset_ns(session_start_ns)
        utc_hour = 8 - offset_ns // (3600 * 1_000_000_000)
        dt = datetime.fromtimestamp(session_start_ns / 1e9, tz=timezone.utc)
        rth_date = (dt + timedelta(days=1)).date()
        rth_open_utc = datetime(
            rth_date.year, rth_date.month, rth_date.day,
            utc_hour, 30, tzinfo=timezone.utc
        )
        return int(rth_open_utc.timestamp() * 1_000_000_000)

    def _compute_strength(self) -> None:
        """Determine if this session's range is strong relative to history."""
        if len(self._range_widths) < 5:
            self._is_strong = False
            return
        sorted_widths = sorted(self._range_widths)
        n = len(sorted_widths)
        if n % 2 == 1:
            median = sorted_widths[n // 2]
        else:
            median = (sorted_widths[n // 2 - 1] + sorted_widths[n // 2]) / 2
        if self._range_high is None or self._range_low is None:
            self._is_strong = False
            return
        current = self._range_high - self._range_low
        self._is_strong = current > self.strength_mult * median

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._open_ts = self._rth_open_ns(ctx.session_start)
            self._range_close_ts = self._open_ts + self.range_minutes * 60 * 1_000_000_000
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._trade_taken = False
            self._is_strong = False
            self._entry_bar_count = 0

        if not ctx.actionable:
            return

        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Don't trade in the last hour of RTH (14:00-15:00 Chicago)
        rth_close_ns = self._open_ts + 6 * 3600 * 1_000_000_000 + 30 * 60 * 1_000_000_000
        if bar.close_ts > rth_close_ns - 3600 * 1_000_000_000:
            return

        # Time stop: if we have a position and have held it too long, flatten
        if ctx.position:
            self._entry_bar_count += 1
            if self._entry_bar_count >= self.max_hold_bars:
                ctx.close()
                self._entry_bar_count = 0
                return
        else:
            self._entry_bar_count = 0

        if not self._range_done:
            if bar.close_ts <= self._range_close_ts:
                if self._range_high is None:
                    self._range_high = bar.high
                    self._range_low = bar.low
                else:
                    self._range_high = max(self._range_high, bar.high)
                    self._range_low = min(self._range_low, bar.low)
            else:
                self._range_done = True
                if self._range_high is not None and self._range_low is not None:
                    width = self._range_high - self._range_low
                    self._range_widths.append(width)
                    self._compute_strength()
                    self._check_break(ctx, bar)
            return

        if self._trade_taken or ctx.position or ctx.working_order_ids:
            return

        self._check_break(ctx, bar)

    def _check_break(self, ctx, bar) -> None:
        if self._range_high is None or self._range_low is None:
            return
        if not self._is_strong:
            return

        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        stop = range_width * self.stop_mult
        target = range_width * self.target_mult

        if bar.close > self._range_high:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="continuation-breakout",
            )
            self._trade_taken = True
            self._entry_bar_count = 0
        elif bar.close < self._range_low:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="continuation-breakdown",
            )
            self._trade_taken = True
            self._entry_bar_count = 0

    def on_fill(self, ctx) -> None:
        # Reset the hold counter when a position is opened
        if ctx.fill.position != 0:
            self._entry_bar_count = 0

    def on_lifecycle(self, ctx) -> None:
        pass
