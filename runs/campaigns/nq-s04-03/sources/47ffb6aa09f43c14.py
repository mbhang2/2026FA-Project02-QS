"""Opening-range fade with a hard flat time, no lookahead, no window iteration.

The question: does a break of the first 30 minutes' range continue, or fail
back into it? This proposal tests **fade** — a break beyond the opening range
is faded, expecting price to return inside. The position is exited at
11:00 Chicago or at the session close, whichever comes first.

Locating the open: CME equity-index day session, 08:30–15:15 Chicago.
Chicago offset derived per timestamp via the US DST rule (second Sunday in
March to first Sunday in November).

Flat time: 11:00 Chicago. Any open position is closed on the first bar
closing at or after that time, or at session close if earlier.

Break definition: a 5-minute bar closes beyond the opening range high by at
least `break_mult` times the range width (for a short fade) or below the low
by the same amount (for a long fade). Entry is on that bar's close against
the break, with a protective stop just beyond the break level. One contract.

The opening range is accumulated incrementally from bars whose open_ts falls
in [08:30, 09:00) Chicago. No `ctx.history` window access is used; all state
is kept on `self`, bounded by the 30-minute window.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


class Strategy:
    """Fade of a 30-minute opening-range break, flat by 11:00 Chicago."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        break_mult: float = 0.25,
        stop_buffer: float = 0.10,
        flat_hour: int = 11,
        flat_minute: int = 0,
        size: int = 1,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.break_mult = break_mult
        self.stop_buffer = stop_buffer
        self.flat_hour = flat_hour
        self.flat_minute = flat_minute
        self.size = size

        self._session: int | None = None
        self._range_bars: list = []
        self._range_high: float | None = None
        self._range_low: float | None = None
        self._flat_ts: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _chicago_offset(self, ts_ns: int) -> int:
        """Return the Chicago UTC offset in hours for a nanosecond timestamp."""
        dt = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
        year = dt.year
        mar = datetime(year, 3, 1, tzinfo=timezone.utc)
        first_sunday_mar = mar + timedelta(days=(6 - mar.weekday()) % 7)
        second_sunday_mar = first_sunday_mar + timedelta(days=7)
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
        session_start = ctx.session_start
        chicago_start = self._chicago_ts(session_start)
        start_dt = datetime.fromtimestamp(chicago_start / 1_000_000_000, tz=timezone.utc)
        chicago_date = start_dt.date()
        flat_chicago = datetime(
            chicago_date.year,
            chicago_date.month,
            chicago_date.day,
            self.flat_hour,
            self.flat_minute,
            tzinfo=timezone.utc,
        )
        flat_chicago_ns = int(flat_chicago.timestamp() * 1_000_000_000)
        offset_hours = self._chicago_offset(flat_chicago_ns)
        return flat_chicago_ns - offset_hours * 3_600_000_000_000

    def on_bars(self, ctx) -> None:
        # Reset per-session state when the session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._range_bars = []
            self._range_high = None
            self._range_low = None
            self._flat_ts = self._compute_flat_ts(ctx)

        # Exit at the flat time: first bar closing at or after it.
        if ctx.position and self._flat_ts is not None and ctx.ts >= self._flat_ts:
            ctx.close()
            return

        # If we have not yet finalized the opening range, collect bars that
        # fall within the opening window.
        if self._range_high is None:
            if self.interval not in ctx.bars:
                return
            bar = ctx.bars[self.interval]
            h, m = self._chicago_hour_minute(bar.open_ts)
            minutes = h * 60 + m
            if 8 * 60 + 30 <= minutes < 8 * 60 + 30 + self.range_minutes:
                self._range_bars.append(bar)
            # Check whether the opening window is complete: a bar with
            # close_ts at or after 09:00 Chicago has closed.
            h_close, m_close = self._chicago_hour_minute(bar.close_ts)
            if h_close * 60 + m_close >= 8 * 60 + 30 + self.range_minutes:
                if not self._range_bars:
                    return
                self._range_high = max(b.high for b in self._range_bars)
                self._range_low = min(b.low for b in self._range_bars)
            return

        # We have a range. Look for a fade signal on the just-closed bar.
        if self.interval not in ctx.bars:
            return
        if ctx.position or ctx.working_order_ids:
            return

        bar = ctx.bars[self.interval]
        assert self._range_high is not None and self._range_low is not None
        width = self._range_high - self._range_low
        if width <= 0:
            return

        required = self.break_mult * width
        # Fade a break above the range: short, expecting a return inside.
        if bar.close > self._range_high + required:
            stop_level = self._range_high + self.stop_buffer * width
            stop_level = max(stop_level, bar.close + 0.25)
            ctx.sell(
                size=self.size,
                stop=stop_level,
                tag="fade-short",
            )
        # Fade a break below the range: long, expecting a bounce back inside.
        elif bar.close < self._range_low - required:
            stop_level = self._range_low - self.stop_buffer * width
            stop_level = min(stop_level, bar.close - 0.25)
            ctx.buy(
                size=self.size,
                stop=stop_level,
                tag="fade-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
