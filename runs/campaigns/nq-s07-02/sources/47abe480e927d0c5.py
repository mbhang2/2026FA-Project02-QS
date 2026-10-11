"""Opening-range fade: when price leaves the regular-hours opening range, the
range wins.

This is the opposite answer to the breakout attempt. Instead of buying strength
above the opening range and selling weakness below it, this strategy fades the
departure: it sells the first close above the opening range plus a
volatility-scaled buffer and buys the first close below it minus the buffer.

Definitions, all explicit:
  * Opening range = high/low of 5m bars whose open_ts is inside the first 30
    minutes of the regular-hours session (08:30-09:00 America/Chicago).
  * "Left" the range = a 5m close beyond the range by a volatility-scaled
    buffer. Volatility is the average high-low range of the last 20 completed
    daily bars, with a training-split fallback so the first sessions can trade.
  * Exit = engine-armed bracket. The stop is scaled from volatility and range
    width. The target is the midpoint of the opening range, with a
    reward-to-risk floor.
  * Undecided = a position still open after the time horizon is closed by a
    market order. The session is also flattened with a margin before the
    regular-hours close, so positions do not ride into Globex overnight.

Trades at most once per session, and only during the first two hours of RTH.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from src.core.types import NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        daily_interval: str = "1d",
        range_minutes: int = 30,
        entry_window_minutes: int = 120,
        max_hold_minutes: int = 75,
        flatten_margin_minutes: int = 5,
        trailing_days: int = 20,
        breakout_vol_mult: float = 0.04,
        stop_mult: float = 2.5,
        min_stop_frac: float = 0.5,
        min_stop_points: float = 2.0,
        reward_risk: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.range_minutes = range_minutes
        self.entry_window_minutes = entry_window_minutes
        self.max_hold_minutes = max_hold_minutes
        self.flatten_margin_minutes = flatten_margin_minutes
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.min_stop_points = min_stop_points
        self.reward_risk = reward_risk
        self.size = size

        self.range_ns = range_minutes * 60 * NS_PER_SECOND
        self.entry_window_ns = entry_window_minutes * 60 * NS_PER_SECOND
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.flatten_margin_ns = flatten_margin_minutes * 60 * NS_PER_SECOND

        self._fallback_daily_vol: float | None = None

        self._session: int | None = None
        self._rth_open_ns: int = 0
        self._rth_close_ns: int = 0
        self._range_high: float | None = None
        self._range_low: float | None = None
        self._range_done: bool = False
        self._daily_vol: float | None = None
        self._deadline_ns: int | None = None
        self._traded_this_session: bool = False
        self._close_queued: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

        # Seed a fallback volatility estimate from the training split so the
        # first scored sessions are not forced to sit out the trailing window.
        try:
            daily_bars = ctx.train_bars(self.daily_interval)
        except Exception:
            daily_bars = ()

        if daily_bars:
            recent = daily_bars[-self.trailing_days:]
            ranges = [b.high - b.low for b in recent if b.high > b.low]
            if ranges:
                self._fallback_daily_vol = sum(ranges) / len(ranges)

    def _rth_bounds(self, session_start_ns: int) -> tuple[int, int]:
        chicago = ZoneInfo("America/Chicago")
        dt = datetime.fromtimestamp(
            session_start_ns // NS_PER_SECOND, tz=timezone.utc
        ).astimezone(chicago)

        # The CME session opened at 17:00 CT on dt.date(), so regular-hours
        # open is 08:30 CT on the following calendar day and close is 15:15 CT.
        rth_date = dt.date() + timedelta(days=1)
        rth_open = datetime.combine(rth_date, time(8, 30), tzinfo=chicago)
        rth_close = datetime.combine(rth_date, time(15, 15), tzinfo=chicago)
        return (
            int(rth_open.timestamp() * NS_PER_SECOND),
            int(rth_close.timestamp() * NS_PER_SECOND),
        )

    def _begin_session(self, session: int, session_start_ns: int) -> None:
        self._session = session
        self._rth_open_ns, self._rth_close_ns = self._rth_bounds(session_start_ns)
        self._range_high = None
        self._range_low = None
        self._range_done = False
        self._daily_vol = None
        self._deadline_ns = None
        self._traded_this_session = False
        self._close_queued = False

    def _update_daily_vol(self, ctx) -> None:
        hist = ctx.history(self.daily_interval)
        if len(hist) >= self.trailing_days:
            days = hist.last(self.trailing_days)
            ranges = [b.high - b.low for b in days]
            self._daily_vol = sum(ranges) / len(ranges)
        elif self._fallback_daily_vol is not None:
            self._daily_vol = self._fallback_daily_vol
        else:
            self._daily_vol = None

    def _update_range(self, bar) -> None:
        if self._range_high is None or bar.high > self._range_high:
            self._range_high = bar.high
        if self._range_low is None or bar.low < self._range_low:
            self._range_low = bar.low

    def _flatten_once(self, ctx) -> None:
        """Queue a flatten exactly once; a pending close is never re-cancelled."""
        if self._close_queued:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position:
            oid = ctx.close()
            if oid is not None:
                self._close_queued = True

    def _manage_open(self, ctx) -> None:
        if self._close_queued:
            return
        if not ctx.actionable:
            return

        if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
            self._flatten_once(ctx)
            return

        # Always flat before the regular-hours close so a position does not
        # ride into the Globex overnight.
        if ctx.ts >= self._rth_close_ns - self.flatten_margin_ns:
            self._flatten_once(ctx)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._begin_session(ctx.session, ctx.session_start)

        if self._daily_vol is None:
            self._update_daily_vol(ctx)

        rth_open = self._rth_open_ns
        range_end = rth_open + self.range_ns

        if bar.open_ts < rth_open:
            return

        # Accumulate the opening range while still inside the formation window.
        if bar.open_ts < range_end:
            self._update_range(bar)
            return

        if not self._range_done:
            self._range_done = True

        if not ctx.actionable:
            return

        if ctx.position:
            self._manage_open(ctx)
            return

        if ctx.working_order_ids:
            return

        if self._traded_this_session:
            return

        if bar.open_ts >= rth_open + self.entry_window_ns:
            return

        if self._range_high is None or self._range_low is None:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        if buffer <= 0:
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        if range_width <= 0:
            return

        stop_off = max(
            self.stop_mult * buffer,
            self.min_stop_frac * range_width,
            self.min_stop_points,
        )

        side = 0
        target_off = 0.0

        if bar.close < self._range_low - buffer:
            # Fade a downside breakout: buy the weakness.
            side = 1
            distance_to_range = self._range_low - bar.close
            target_off = max(
                distance_to_range + 0.5 * range_width,
                stop_off * self.reward_risk,
            )
        elif bar.close > self._range_high + buffer:
            # Fade an upside breakout: sell the strength.
            side = -1
            distance_to_range = bar.close - self._range_high
            target_off = max(
                distance_to_range + 0.5 * range_width,
                stop_off * self.reward_risk,
            )

        if side == 0:
            return

        if side > 0:
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-fade-long",
            )
        else:
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-fade-short",
            )

        if oid is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._deadline_ns = None
            self._close_queued = False
            return

        self._close_queued = False
        if self._deadline_ns is None:
            hard_deadline = self._rth_close_ns - self.flatten_margin_ns
            deadline = ctx.ts + self.max_hold_ns
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
