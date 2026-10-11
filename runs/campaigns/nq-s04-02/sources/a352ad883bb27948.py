"""Opening-range breakout: does a break of the first 30 minutes continue or fail?

This strategy answers the campaign question directly. It locates the regular-hours
open (08:30 Chicago, offset derived per-session from the date), builds the opening
range from the first 30 minutes of RTH, and trades the first break of that range
after the range completes.

The claim: a break of the opening range tends to fail back into the range in the
short term. We fade the break -- sell a break above the range high, buy a break
below the range low -- with a bracket exit that captures a quick reversion. The
holding period is deliberately short (a few bars at most), keeping the position
within the hours the claim covers.

What counts as a break: the first bar after the opening range whose close is
outside the range. The entry is placed at the break bar's close, with a stop and
target sized from the opening range's own width. This scales the trade to the
session's volatility rather than a fixed number of points.

Time convention: Chicago offset is derived from the session date using the
standard US DST rule (second Sunday in March to first Sunday in November).
The RTH open is 08:30 Chicago, the close is 15:00 Chicago (cash-equity hours).
The opening range is the first 30 minutes: 08:30 to 09:00 Chicago.

Exits: bracket with stop and target both set to 0.5x the opening range width.
This keeps the holding period short and the trade within the hours the claim
covers. No position is carried into the afternoon session.

Weekday and month splits are reported by the run record; the strategy itself
does not condition on them.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


class Strategy:
    """Fade the first break of the 30-minute opening range."""

    def __init__(self, *, interval: str = "5m", range_minutes: int = 30,
                 stop_mult: float = 0.5, target_mult: float = 0.5,
                 size: int = 1):
        self.interval = interval
        self.range_minutes = range_minutes
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

        # Session state
        self._session: int | None = None
        self._open_ts: int | None = None          # RTH open, ns UTC
        self._range_close_ts: int | None = None   # RTH open + range_minutes, ns UTC
        self._range_high: float | None = None
        self._range_low: float | None = None
        self._range_done: bool = False
        self._trade_taken: bool = False

    @staticmethod
    def _chicago_offset_ns(session_start_ns: int) -> int:
        """Return the Chicago UTC offset (seconds) as nanoseconds for a session.

        The session start is in UTC. Chicago is -6 in winter, -5 in summer.
        DST rule: second Sunday in March to first Sunday in November.
        """
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
        # 08:30 Chicago = 08:30 - offset hours UTC
        open_hour = 8
        open_minute = 30
        # Convert Chicago 08:30 to UTC: UTC = Chicago - offset
        # offset is negative (-5 or -6 hours), so UTC = 08:30 + 5 or 6 hours
        utc_hour = open_hour - offset_ns // (3600 * 1_000_000_000)
        utc_minute = open_minute

        # Get the date in UTC for this session
        dt = datetime.fromtimestamp(session_start_ns / 1e9, tz=timezone.utc)
        # The RTH open on the session's calendar date. Session start is 17:00
        # previous day Chicago, which is 22:00 or 23:00 UTC previous day.
        # The RTH open is on the calendar day that contains the session's
        # regular hours. For a session starting at 17:00 CT on day D-1,
        # RTH open is 08:30 CT on day D.
        # In UTC, the session start is on day D-1 at 22:00 or 23:00.
        # So the RTH open date is the UTC date of session_start + 1 day.
        rth_date = (dt + timedelta(days=1)).date()
        rth_open_utc = datetime(
            rth_date.year, rth_date.month, rth_date.day,
            utc_hour, utc_minute, tzinfo=timezone.utc
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

        if not ctx.actionable:
            return

        # Only act on our interval's bars
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Before range completion: accumulate the opening range
        if not self._range_done:
            if self._range_close_ts is not None and bar.close_ts <= self._range_close_ts:
                # This bar is within the opening range window
                if self._range_high is None:
                    self._range_high = bar.high
                    self._range_low = bar.low
                else:
                    self._range_high = max(self._range_high, bar.high)
                    self._range_low = min(self._range_low, bar.low)
            else:
                # This bar closes after the range window; the range is done
                self._range_done = True
                # The range is complete; now look for a break
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

        # A break is a close outside the range
        if bar.close > self._range_high:
            # Fade the upside break: sell
            stop = range_width * self.stop_mult
            target = range_width * self.target_mult
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="fade-breakout",
            )
            self._trade_taken = True
        elif bar.close < self._range_low:
            # Fade the downside break: buy
            stop = range_width * self.stop_mult
            target = range_width * self.target_mult
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="fade-breakdown",
            )
            self._trade_taken = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
