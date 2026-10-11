"""Opening-range breakout with one filled entry per session.

The strategy asks: when price leaves the regular-hours opening range, does the
move hold or does the range win?  This version takes the "move holds" side,
trading the first volatility-scaled departure after the opening range is known.

It is deliberately close to the completed build that scored -0.0068, with one
change of substance: after one entry has filled, no further entry may be placed
that session.  The earlier build traded 483 times over 384 sessions, so it was
re-entering after bracket fills and paying commission without a corresponding
edge.  Capping entries reduces turnover while preserving exposure.

The volatility estimate is seeded from training-split daily bars, so early
scored sessions are not inert before 20 live daily bars exist.  If no daily
volatility estimate is available, a fallback buffer proportional to the opening
range is used; this path exists only when both the training split and live
history provide no estimate.

Definitions:
  * opening range = high/low of the first 30 minutes of the regular-hours
    session, built from 5m bars
  * volatility = average high-low range of the last 20 completed daily bars
  * departure = a 5m close beyond the range by a buffer proportional to that
    volatility
  * direction = trade the departure: long above the upper buffer, short below
    the lower buffer
  * exit = engine-armed bracket, plus a time horizon that flattens the position
    after 75 minutes or five minutes before the regular-hours close, whichever
    is earlier, so an undecided departure is not silently counted as a range
    win or as a move that holds

The 5m subscription uses emit_empty=True so the time horizon can fire even when
the session goes quiet.
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
        breakout_vol_mult: float = 0.04,
        stop_mult: float = 2.5,
        min_stop_frac: float = 0.5,
        reward_risk: float = 1.5,
        target_mult: float = 3.0,
        size: int = 1,
        max_hold_minutes: int = 75,
        entry_window_minutes: int = 120,
        max_trades_per_session: int = 1,
        fallback_buffer_frac: float = 0.35,
        min_fallback_buffer: float = 2.0,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.reward_risk = reward_risk
        self.target_mult = target_mult
        self.max_trades_per_session = max_trades_per_session
        self.fallback_buffer_frac = fallback_buffer_frac
        self.min_fallback_buffer = min_fallback_buffer

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
        self._entry_fills = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

        # Seed the volatility estimate from the training split.  This is fit on
        # data strictly before the scored split, and keeps the first live
        # sessions tradable before 20 live daily bars have accumulated.
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
            self._entry_fills = 0

        self._update_daily_vol(ctx)

        rth_open = ctx.session_start + self.rth_open_offset_ns
        range_end = rth_open + self.range_ns
        entry_end = rth_open + self.entry_window_ns

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

        # Warmup sessions deliver real bars but refuse orders.
        if not ctx.actionable:
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

        # If anything is still working, it is either a bracket from a previous
        # entry or a state we are not ready to disturb.
        if ctx.working_order_ids:
            return

        # An empty bar after the range cannot be a new departure.
        if bar.tick_count == 0:
            return

        # One filled entry per session.  Re-entry after bracket fills was the
        # main source of excess turnover in the previous completed build.
        if self._entry_fills >= self.max_trades_per_session:
            return

        if self._range_high is None or self._range_low is None:
            return
        if bar.close_ts > entry_end:
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        if range_width <= 0:
            return

        # The normal case: a volatility-scaled departure buffer.  The fallback
        # is only used when no daily volatility estimate exists, which happens
        # when both the training split and live history provide none.
        if self._daily_vol is not None and self._daily_vol > 0:
            buffer = self.breakout_vol_mult * self._daily_vol
        else:
            buffer = max(self.fallback_buffer_frac * range_width, self.min_fallback_buffer)

        side = 0
        if bar.close > self._range_high + buffer:
            side = 1
        elif bar.close < self._range_low - buffer:
            side = -1
        if side == 0:
            return

        stop_off = max(self.stop_mult * buffer, self.min_stop_frac * range_width, 2.0)
        target_off = max(self.target_mult * buffer, stop_off * self.reward_risk)
        target_off = max(target_off, stop_off * 1.1)

        if side > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-short",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position == 0:
            self._deadline_ns = None
            return

        # A fill with non-zero position is an entry fill in this strategy,
        # because we never pyramid and never add to an existing position.
        if self._entry_fills < self.max_trades_per_session:
            self._entry_fills += 1

        if self._deadline_ns is None:
            rth_close = ctx.session_start + self.rth_close_offset_ns
            deadline = ctx.ts + self.max_hold_ns
            hard_deadline = rth_close - 5 * 60 * NS_PER_SECOND
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
