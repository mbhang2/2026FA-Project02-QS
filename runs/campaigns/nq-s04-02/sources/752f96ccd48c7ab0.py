"""Opening-range breakout: fade the break with a volatility-scaled stop and a
mandatory time exit.

This strategy answers the campaign question by fading (betting against) the
first break of the 30-minute opening range. The hypothesis is that a break of
the range tends to fail back into it in the short term. We fade the break --
sell a break above the range high, buy a break below the range low -- with a
bracket exit that captures a quick reversion.

The key changes from previous versions:
1. The target is set to 0.25x the opening range width, taking profit sooner.
2. A mandatory time stop is added: if the trade is still open 30 minutes after
   entry, we flatten it at market. This keeps the holding period short and
   prevents the position from drifting into hours the claim does not cover.
3. The stop is scaled by the opening range width (0.5x), as before.

What counts as a break: the first bar after the opening range whose close is
outside the range. The entry is placed at the break bar's close, with a stop
and target sized from the opening range's own width.

Time convention: Chicago offset is derived from the session date using the
standard US DST rule (second Sunday in March to first Sunday in November).
The RTH open is 08:30 Chicago, the close is 15:00 Chicago (cash-equity hours).
The opening range is the first 30 minutes: 08:30 to 09:00 Chicago.

Exits: bracket with stop at 0.5x the opening range width and target at 0.25x
the opening range width. Additionally, a time stop flattens any open position
30 minutes after entry. No position is carried into the afternoon session.

Weekday and month splits are reported by the run record; the strategy itself
does not condition on them.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


class Strategy:
    """Fade the first break of the 30-minute opening range, with a time stop."""

    def __init__(self, *, interval: str = "5m", range_minutes: int = 30,
                 stop_mult: float = 0.5, target_mult: float = 0.25,
                 time_stop_minutes: int = 30, size: int = 1):
        self.interval = interval
        self.range_minutes = range_minutes
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.time_stop_minutes = time_stop_minutes
        self.size = size

        # Session state
        self._session: int | None = None
        self._open_ts: int = 0
        self._range_close_ts: int = 0
        self._range_high: float | None = None
        self._range_low: float | None = None
        self._range_done: bool = False
        self._trade_taken: bool = False
        self._entry_ts: int = 0
        self._time_stop_ts: int = 0

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
            self._trade_taken = False
            self._entry_ts = 0
            self._time_stop_ts = 0

        if not ctx.actionable:
            return

        # Only act on our interval's bars
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Time stop check: if we have an open position and the time stop has
        # passed, flatten it.
        if self._trade_taken and ctx.position and ctx.ts >= self._time_stop_ts:
            ctx.close(tag="time-stop")
            self._trade_taken = False  # allow a new trade? No, one trade per session
            return

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

        # Range done: look for a break
        if self._trade_taken or ctx.position or ctx.working_order_ids:
            return

        self._check_break(ctx, bar)

    def _check_break(self, ctx, bar) -> None:
        """Check if this bar breaks the opening range and trade the fade."""
        if self._range_high is None or self._range_low is None:
            return

        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        stop = range_width * self.stop_mult
        target = range_width * self.target_mult

        if bar.close > self._range_high:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="fade-breakout",
            )
            self._trade_taken = True
            self._entry_ts = ctx.ts
            self._time_stop_ts = self._entry_ts + self.time_stop_minutes * 60 * 1_000_000_000
        elif bar.close < self._range_low:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="fade-breakdown",
            )
            self._trade_taken = True
            self._entry_ts = ctx.ts
            self._time_stop_ts = self._entry_ts + self.time_stop_minutes * 60 * 1_000_000_000

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
