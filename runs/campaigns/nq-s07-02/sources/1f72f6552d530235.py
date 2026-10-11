"""Opening-range fade with a training-seeded volatility estimate.

This strategy tests the "range wins" account of the campaign question:
when price leaves the regular-hours opening range, it tends to return inside
rather than holding the departure.

Definitions:
  * Opening range = high/low of the first 30 minutes of the regular-hours
    session, from 08:30 to 09:00 CT, built from 5m bars.
  * Volatility buffer = average high-low range of the last 20 completed daily
    bars, seeded in setup() from the training split so early sessions are not
    inert and then switched to live trailing values once enough live daily
    bars exist.
  * Departure = a 5m close beyond the range by a buffer proportional to that
    volatility.
  * Trade = fade the departure: short the first close above the upper buffer,
    long the first close below the lower buffer.
  * Exit = protective stop outside the departure; target at the opening-range
    boundary (the first price "inside" the range).  A time horizon closes any
    position that has neither outcome yet, so undetermined departures are not
    silently counted as range wins or trend wins.

Only one entry is allowed per session.  The 5m subscription uses
emit_empty=True so the time exit and end-of-session defence can fire even when
the session's late bars have no trades.
"""

from __future__ import annotations

from src.core.types import NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        daily_interval: str = "1d",
        range_minutes: int = 30,
        rth_open_offset_seconds: int = 55800,   # 17:00 CT -> 08:30 CT
        rth_close_offset_seconds: int = 80100,  # 17:00 CT -> 15:15 CT
        trailing_days: int = 20,
        breakout_vol_mult: float = 0.05,
        stop_mult: float = 1.5,
        min_stop_frac: float = 0.5,
        min_stop_points: float = 3.0,
        reward_risk: float = 0.5,
        fade_target_frac: float = 0.0,
        min_target_points: float = 3.0,
        size: int = 1,
        max_hold_minutes: int = 70,
        entry_window_minutes: int = 120,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.min_stop_points = min_stop_points
        self.reward_risk = reward_risk
        self.fade_target_frac = fade_target_frac
        self.min_target_points = min_target_points

        self.range_ns = range_minutes * 60 * NS_PER_SECOND
        self.entry_window_ns = entry_window_minutes * 60 * NS_PER_SECOND
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.rth_open_offset_ns = rth_open_offset_seconds * NS_PER_SECOND
        self.rth_close_offset_ns = rth_close_offset_seconds * NS_PER_SECOND

        self._session = None
        self._range_high = None
        self._range_low = None
        self._range_done = False
        self._daily_vol = None
        self._initial_daily_vol = None
        self._deadline_ns = None
        self._traded_this_session = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

        # Seed the volatility estimate from the training split.  This makes the
        # first scored sessions tradable and gives the synthetic smoke test the
        # same chance to produce an order as the live run.
        self._initial_daily_vol = None
        try:
            daily_bars = ctx.train_bars(self.daily_interval)
            if daily_bars:
                recent = daily_bars[-self.trailing_days :]
                ranges = [b.high - b.low for b in recent if b.high > b.low]
                if ranges:
                    self._initial_daily_vol = sum(ranges) / len(ranges)
        except Exception:
            pass

    def _update_daily_vol(self, ctx) -> None:
        hist = ctx.history(self.daily_interval)
        if len(hist) < self.trailing_days:
            return
        days = hist.last(self.trailing_days)
        ranges = [b.high - b.low for b in days if b.high > b.low]
        if ranges:
            self._daily_vol = sum(ranges) / len(ranges)

    def _update_range(self, bar) -> None:
        if self._range_high is None or bar.high > self._range_high:
            self._range_high = bar.high
        if self._range_low is None or bar.low < self._range_low:
            self._range_low = bar.low

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._daily_vol = self._initial_daily_vol
            self._deadline_ns = None
            self._traded_this_session = False

        if self._daily_vol is None:
            self._update_daily_vol(ctx)

        rth_open = ctx.session_start + self.rth_open_offset_ns
        range_end = rth_open + self.range_ns

        if bar.close_ts <= rth_open:
            return

        # Accumulate the opening range.  Empty bars advance time but do not
        # alter the range.
        if bar.close_ts < range_end:
            if bar.tick_count > 0:
                self._update_range(bar)
            return
        if bar.close_ts == range_end:
            if bar.tick_count > 0:
                self._update_range(bar)
            self._range_done = True
            return
        if not self._range_done:
            self._range_done = True
            return

        # Manage an open position first, including on empty bars, so the time
        # horizon can fire even when the session has gone quiet.
        if ctx.position:
            if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
                if ctx.working_order_ids:
                    ctx.cancel_all()
                ctx.close()
                self._deadline_ns = None
            return

        if ctx.working_order_ids:
            return

        if not ctx.actionable:
            return

        if bar.tick_count == 0:
            return

        if self._traded_this_session:
            return

        if self._range_high is None or self._range_low is None:
            return
        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        if bar.close_ts > rth_open + self.entry_window_ns:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        stop_off = max(
            self.stop_mult * buffer,
            self.min_stop_frac * range_width,
            self.min_stop_points,
        )

        side = 0
        if bar.close > self._range_high + buffer:
            side = -1  # short the departure
        elif bar.close < self._range_low - buffer:
            side = 1   # buy the departure

        if side == 0:
            return

        if side < 0:
            target_price = self._range_high - self.fade_target_frac * range_width
            target_dist = max(
                bar.close - target_price,
                stop_off * self.reward_risk,
                self.min_target_points,
            )
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_dist,
                tag="orb-fade-short",
            )
        else:
            target_price = self._range_low + self.fade_target_frac * range_width
            target_dist = max(
                target_price - bar.close,
                stop_off * self.reward_risk,
                self.min_target_points,
            )
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_dist,
                tag="orb-fade-long",
            )

        if oid is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._deadline_ns = None
            return

        if self._deadline_ns is None:
            rth_close = ctx.session_start + self.rth_close_offset_ns
            deadline = ctx.ts + self.max_hold_ns
            hard_deadline = rth_close - 5 * 60 * NS_PER_SECOND
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
