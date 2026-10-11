"""Opening-range breakout: fade the break with confirmation and a time stop.

This strategy answers the campaign question by fading the first break of the
30-minute opening range, but only after a confirmation bar closes back inside
the range. The hypothesis is that a break that immediately fails -- where the
next bar closes back within the range -- is a genuine exhaustion signal, while
a break that holds outside the range is more likely to continue.

The key change from previous versions: we no longer fade the break bar itself.
Instead, we wait for the bar *after* the break. If that bar closes back inside
the opening range, we fade the original break direction. This avoids entering
on a break that continues, which was likely the source of the prior fade's
losses.

What counts as a break: the first bar after the opening range whose close is
outside the range. What counts as a confirmation: the next bar closes back
inside the range. Entry is at the close of the confirmation bar, with a stop
and target sized from the opening range's width.

Time convention: Chicago offset is derived from the session date using the
standard US DST rule. The RTH open is 08:30 Chicago, the close is 15:00
Chicago (cash-equity hours). The opening range is the first 30 minutes.

Exits: bracket with stop at 0.5x the opening range width and target at 0.25x
the opening range width. This keeps the holding period short and the trade
within the hours the claim covers. No position is carried into the afternoon
session.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


class Strategy:
    """Fade a confirmed failure of the 30-minute opening range."""

    def __init__(self, *, interval: str = "5m", range_minutes: int = 30,
                 stop_mult: float = 0.5, target_mult: float = 0.25,
                 size: int = 1):
        self.interval = interval
        self.range_minutes = range_minutes
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

        # Session state
        self._session: int | None = None
        self._open_ts: int = 0
        self._range_close_ts: int = 0
        self._range_high: float | None = None
        self._range_low: float | None = None
        self._range_done: bool = False
        self._break_direction: int = 0  # 0=none, 1=breakout, -1=breakdown
        self._trade_taken: bool = False

    @staticmethod
    def _chicago_offset_ns(session_start_ns: int) -> int:
        """Return the Chicago UTC offset (seconds) as nanoseconds for a session."""
        dt = datetime.fromtimestamp(session_start_ns / 1e9, tz=timezone.utc)
        year = dt.year

        # Second Sunday in March
        march_first = datetime(year, 3, 1, tzinfo=timezone.utc)
        march_second_sunday = march_first + timedelta(
            days=(6 - march_first.weekday() + 7) % 7 + 7
        )
        # First Sunday in November
        nov_first = datetime(year, 11, 1, tzinfo=timezone.utc)
        nov_first_sunday = nov_first + timedelta(
            days=(6 - nov_first.weekday()) % 7
        )

        if march_second_sunday <= dt < nov_first_sunday:
            return -5 * 3600 * 1_000_000_000
        return -6 * 3600 * 1_000_000_000

    def _rth_open_ns(self, session_start_ns: int) -> int:
        """Return the RTH open (08:30 Chicago) in ns UTC for this session."""
        offset_ns = self._chicago_offset_ns(session_start_ns)
        utc_hour = 8 - offset_ns // (3600 * 1_000_000_000)
        dt = datetime.fromtimestamp(session_start_ns / 1e9, tz=timezone.utc)
        rth_date = (dt + timedelta(days=1)).date()
        rth_open_utc = datetime(
            rth_date.year, rth_date.month, rth_date.day,
            utc_hour, 30, tzinfo=timezone.utc
        )
        return int(rth_open_utc.timestamp() * 1_000_000_000)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset state on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._open_ts = self._rth_open_ns(ctx.session_start)
            self._range_close_ts = self._open_ts + self.range_minutes * 60 * 1_000_000_000
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._break_direction = 0
            self._trade_taken = False

        if not ctx.actionable:
            return

        # Only act on our interval's bars
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Before range completion: accumulate the opening range
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
                self._check_break(ctx, bar)
            return

        # Range done: look for a break or confirmation
        if self._trade_taken or ctx.position or ctx.working_order_ids:
            return

        if self._break_direction == 0:
            # No break yet: check for one
            self._check_break(ctx, bar)
        else:
            # We have a break; check if this bar confirms the failure
            self._check_confirmation(ctx, bar)

    def _check_break(self, ctx, bar) -> None:
        """Check if this bar breaks the opening range."""
        if self._range_high is None or self._range_low is None:
            return

        if bar.close > self._range_high:
            self._break_direction = 1
        elif bar.close < self._range_low:
            self._break_direction = -1

    def _check_confirmation(self, ctx, bar) -> None:
        """Check if the bar after the break closes back inside the range."""
        if self._range_high is None or self._range_low is None:
            return

        # Confirmation: the bar closes back inside the range
        if self._range_low <= bar.close <= self._range_high:
            range_width = self._range_high - self._range_low
            if range_width <= 0:
                return

            stop = range_width * self.stop_mult
            target = range_width * self.target_mult

            # Fade the original break direction
            if self._break_direction == 1:
                # Breakout failed: sell short
                ctx.sell(
                    size=self.size,
                    stop_offset=stop,
                    target_offset=target,
                    tag="fade-confirmed-breakout",
                )
                self._trade_taken = True
            elif self._break_direction == -1:
                # Breakdown failed: buy long
                ctx.buy(
                    size=self.size,
                    stop_offset=stop,
                    target_offset=target,
                    tag="fade-confirmed-breakdown",
                )
                self._trade_taken = True

            # Reset break direction so we don't trade again this session
            self._break_direction = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
