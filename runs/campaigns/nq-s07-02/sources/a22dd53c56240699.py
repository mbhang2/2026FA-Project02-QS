"""Opening-range breakout with stop-entry, one filled entry per session, live trailing volatility.

This strategy answers "the move holds": when price leaves the regular-hours
opening range it trades the departure direction.  It improves on the best prior
completed build (#0) in three ways:

  * exactly one filled entry per session, so a completed bracket cannot
    immediately re-enter and generate a second round trip;
  * entry is attempted with resting stop orders at the volatility-scaled
    departure levels, rather than waiting for a 5m close beyond the threshold,
    so the trade is triggered by the first intrabar departure and not by a
    delayed close that may already be far past the level;
  * trailing daily volatility is seeded from the training split and then
    updated from live daily bars once enough exist, rather than being computed
    once and frozen.

A time horizon still flattens any position that has neither held enough nor
returned, so undecided departures are not silently counted as range wins or
move holds.  The 5m subscription uses emit_empty=True so the time exit can fire
even when the late session has no trades.
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
        min_stop_points: float = 2.0,
        reward_risk: float = 1.5,
        target_mult: float = 3.0,
        size: int = 1,
        max_hold_minutes: int = 75,
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
        self.target_mult = target_mult

        self.range_ns = range_minutes * 60 * NS_PER_SECOND
        self.entry_window_ns = entry_window_minutes * 60 * NS_PER_SECOND
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.rth_open_offset_ns = rth_open_offset_seconds * NS_PER_SECOND
        self.rth_close_offset_ns = rth_close_offset_seconds * NS_PER_SECOND

        self.long_entry_tag = "orb-entry-long"
        self.short_entry_tag = "orb-entry-short"

        self._session = None
        self._range_high = None
        self._range_low = None
        self._range_done = False
        self._daily_vol = None
        self._initial_daily_vol = None
        self._deadline_ns = None
        self._traded_this_session = False
        self._entry_cancelled = False

    def setup(self, ctx) -> None:
        # emit_empty=True so the time exit can fire even when the late session
        # has no trades.  Empty bars do not affect the opening range.
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

        # Seed the volatility estimate from the training split.  Early scored
        # sessions then have a sane buffer; later sessions use live trailing
        # daily ranges once enough exist.
        self._initial_daily_vol = None
        try:
            daily_bars = ctx.train_bars(self.daily_interval)
            if daily_bars:
                recent = daily_bars[-self.trailing_days :]
                ranges = [b.high - b.low for b in recent if b.high > b.low]
                if ranges:
                    self._initial_daily_vol = sum(ranges) / len(ranges)
        except Exception:
            # Training data is best-effort.  If absent, the strategy falls back
            # to waiting for live daily bars, exactly as the prior build did.
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
        if bar.tick_count == 0:
            return
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
            self._entry_cancelled = False
            self._update_daily_vol(ctx)

        if self._daily_vol is None:
            self._update_daily_vol(ctx)

        rth_open = ctx.session_start + self.rth_open_offset_ns
        range_end = rth_open + self.range_ns

        if bar.close_ts <= rth_open:
            return

        # Accumulate the opening range.  Empty bars advance time but do not
        # alter the range, because their OHLC are carried forward, not traded.
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

        # One entry window.  Past this, cancel any resting entry orders and do
        # not submit new ones.
        if bar.close_ts > rth_open + self.entry_window_ns:
            if ctx.working_order_ids:
                ctx.cancel_all()
            self._entry_cancelled = True
            return

        if ctx.working_order_ids:
            return

        if self._traded_this_session or self._entry_cancelled:
            return

        if self._range_high is None or self._range_low is None:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return
        if bar.tick_count == 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        range_width = max(self._range_high - self._range_low, 0.0)
        stop_off = max(
            self.stop_mult * buffer,
            self.min_stop_frac * range_width,
            self.min_stop_points,
        )
        target_off = max(self.target_mult * buffer, stop_off * self.reward_risk)
        target_off = max(target_off, stop_off * 1.1)

        long_trigger = self._range_high + buffer
        short_trigger = self._range_low - buffer

        # If this bar's close is already beyond a trigger, enter immediately.
        # Otherwise rest stop orders at both departure levels and let the first
        # intrabar departure fill the one trade for the session.
        if bar.close > long_trigger:
            ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-long-market",
            )
        elif bar.close < short_trigger:
            ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-short-market",
            )
        else:
            ctx.buy(
                size=self.size,
                type="stop",
                trigger=long_trigger,
                stop_offset=stop_off,
                target_offset=target_off,
                tag=self.long_entry_tag,
            )
            ctx.sell(
                size=self.size,
                type="stop",
                trigger=short_trigger,
                stop_offset=stop_off,
                target_offset=target_off,
                tag=self.short_entry_tag,
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._deadline_ns = None
            return

        # This is an entry fill, so the session's one filled entry is consumed.
        self._traded_this_session = True

        # Cancel the unused resting entry order, if it is still working.
        if fill.position > 0:
            for order in ctx.find_orders(tag=self.short_entry_tag):
                ctx.cancel(order.id)
        else:
            for order in ctx.find_orders(tag=self.long_entry_tag):
                ctx.cancel(order.id)

        if self._deadline_ns is None:
            rth_close = ctx.session_start + self.rth_close_offset_ns
            deadline = ctx.ts + self.max_hold_ns
            hard_deadline = rth_close - 5 * 60 * NS_PER_SECOND
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
