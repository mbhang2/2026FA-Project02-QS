"""Opening-range continuation, 30 minutes, with a stated flat time.

Question: does a break of the first 30 minutes' range continue, or fail back
into it? This proposal tests **continuation**: a bar closing beyond the range
enters in the direction of the break. The exit is a time stop, not a bracket
target, because the question is about what the next hour or two do, not about
the afternoon.

Locating the open: CME equity-index day session, 08:30-15:15 Chicago. The
Chicago offset is derived per timestamp from the session's date using the
US daylight-saving rule (second Sunday in March to first Sunday in November).
The opening range is built from 5-minute bars whose open_ts falls in
[08:30, 09:00) Chicago.

Flat time: 11:00 Chicago. A continuation claim about the first half hour is a
claim about the next two hours at most; anything held past 11:00 is answering a
different question. The strategy exits any open position on the first bar
closing at or after 11:00 Chicago, or at the session close if that comes first.

Break definition: a 5-minute bar closes beyond the opening range high or low.
Entry is on that bar's close, in the direction of the break. The range is
scaled: a break must be at least 0.25 times the range width beyond the boundary,
so a trivial poke through does not count. The protective stop is the opposite
side of the opening range; the position is also exited at the flat time.

Sizing: one contract, because the question is about direction, not about
position management.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


class Strategy:
    """Continuation of a 30-minute opening-range break, flat by 11:00 Chicago."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        break_mult: float = 0.25,
        flat_hour: int = 11,
        flat_minute: int = 0,
        size: int = 1,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.break_mult = break_mult
        self.flat_hour = flat_hour
        self.flat_minute = flat_minute
        self.size = size

        # State kept across callbacks.
        self._session: int | None = None
        self._range_high: float | None = None
        self._range_low: float | None = None
        self._flat_ts: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _chicago_offset(self, ts_ns: int) -> int:
        """Return the Chicago UTC offset in hours for a nanosecond timestamp.

        US daylight saving: second Sunday in March to first Sunday in November.
        Outside that, standard time (-6). Inside, daylight time (-5).
        """
        dt = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
        year = dt.year
        # Second Sunday in March.
        mar = datetime(year, 3, 1, tzinfo=timezone.utc)
        first_sunday_mar = mar + timedelta(days=(6 - mar.weekday()) % 7)
        second_sunday_mar = first_sunday_mar + timedelta(days=7)
        # First Sunday in November.
        nov = datetime(year, 11, 1, tzinfo=timezone.utc)
        first_sunday_nov = nov + timedelta(days=(6 - nov.weekday()) % 7)
        if second_sunday_mar <= dt < first_sunday_nov:
            return -5
        return -6

    def _chicago_ts(self, ts_ns: int) -> int:
        """Convert a UTC nanosecond timestamp to Chicago time as ns since epoch."""
        offset_hours = self._chicago_offset(ts_ns)
        return ts_ns + offset_hours * 3_600_000_000_000

    def _chicago_hour_minute(self, ts_ns: int) -> tuple[int, int]:
        """Return (hour, minute) in Chicago for a UTC ns timestamp."""
        chicago = self._chicago_ts(ts_ns)
        dt = datetime.fromtimestamp(chicago / 1_000_000_000, tz=timezone.utc)
        return dt.hour, dt.minute

    def _compute_flat_ts(self, ctx) -> int:
        """Compute the session's flat timestamp in UTC ns."""
        # Need a Chicago time of flat_hour:flat_minute on the session's date.
        # Use the session start to find the UTC date; the session is the same
        # Chicago calendar day from 17:00 previous to 16:00.
        session_start = ctx.session_start
        # Convert session start to Chicago and get its date.
        chicago_start = self._chicago_ts(session_start)
        start_dt = datetime.fromtimestamp(chicago_start / 1_000_000_000, tz=timezone.utc)
        # The session's Chicago date is the date of the session start in Chicago.
        chicago_date = start_dt.date()
        # Build the flat time in Chicago on that date.
        flat_chicago = datetime(
            chicago_date.year,
            chicago_date.month,
            chicago_date.day,
            self.flat_hour,
            self.flat_minute,
            tzinfo=timezone.utc,
        )
        flat_chicago_ns = int(flat_chicago.timestamp() * 1_000_000_000)
        # Convert Chicago ns back to UTC ns.
        offset_hours = self._chicago_offset(flat_chicago_ns)
        flat_utc_ns = flat_chicago_ns - offset_hours * 3_600_000_000_000
        return flat_utc_ns

    def on_bars(self, ctx) -> None:
        # Reset per-session state when the session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._flat_ts = self._compute_flat_ts(ctx)

        # Exit at the flat time: first bar closing at or after it.
        if ctx.position and self._flat_ts is not None and ctx.ts >= self._flat_ts:
            ctx.close()
            return

        # Build the opening range from the first range_minutes of Chicago time.
        if self._range_high is None:
            history = ctx.history(self.interval)
            # Need enough bars to find the range.
            if len(history) < 1:
                return
            # Collect bars whose open_ts is in the opening window.
            range_bars = []
            for bar in history:
                h, m = self._chicago_hour_minute(bar.open_ts)
                minutes = h * 60 + m
                if 8 * 60 + 30 <= minutes < 8 * 60 + 30 + self.range_minutes:
                    range_bars.append(bar)
            if not range_bars:
                return
            # Wait until the opening window is complete. The last bar of the
            # window closes at 09:00 Chicago, so we need a bar with close_ts
            # at or after that.
            last_range_bar = range_bars[-1]
            h, m = self._chicago_hour_minute(last_range_bar.close_ts)
            if h * 60 + m < 8 * 60 + 30 + self.range_minutes:
                return
            # Compute the range.
            self._range_high = max(bar.high for bar in range_bars)
            self._range_low = min(bar.low for bar in range_bars)
            # Do not act on the bar that completes the range; wait for a break.
            return

        # We have a range. Look for a break on the just-closed bar.
        if self.interval not in ctx.bars:
            return
        if ctx.position or ctx.working_order_ids:
            return  # one position at a time

        bar = ctx.bars[self.interval]
        assert self._range_high is not None and self._range_low is not None
        width = self._range_high - self._range_low
        if width <= 0:
            return

        # Continuation: close beyond the range by at least break_mult * width.
        required = self.break_mult * width
        if bar.close > self._range_high + required:
            ctx.buy(
                size=self.size,
                stop=self._range_low,
                tag="continuation-long",
            )
        elif bar.close < self._range_low - required:
            ctx.sell(
                size=self.size,
                stop=self._range_high,
                tag="continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
