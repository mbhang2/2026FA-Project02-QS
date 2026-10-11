"""Opening-range breakout with stop-entry orders and one filled entry per session.

The strategy asks: when price leaves the regular-hours opening range, does the
move hold or does the range win?

This build takes the "move holds" side of the question and changes *how* the
entry is made.  Previous builds waited for a 5-minute close beyond the range.
That is a delayed, filtered observation of a departure.  This build instead
defines departure as price actually trading through the range boundary plus a
trailing-volatility buffer, and uses resting stop orders to enter at the first
such trade.

Range: high/low of the first 30 minutes of the regular-hours session, built
from 1-minute bars.  Volatility: average daily high-low over the last 20
completed sessions, seeded from the training split and then switched to live
trailing values.  After the opening range is complete, two resting stop orders
are placed: a buy stop above the range and a sell stop below it.  The first one
to trade fills; the sibling is cancelled in on_fill.

Only one filled entry is allowed per session.  A time horizon closes a position
that has neither hit its target nor its stop, so an undecided departure is not
silently counted as either "range wins" or "move holds".  The 1m subscription
uses emit_empty=True so the time exit can fire even on a quiet tape.
"""

from __future__ import annotations

from src.core.types import NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
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
        min_stop_points: float = 2.0,
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
        self.reward_risk = reward_risk
        self.target_mult = target_mult
        self.min_stop_points = min_stop_points

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
        self._entry_orders_placed = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

        # Seed the trailing daily volatility from the training split.  This is
        # training data only, so it carries no lookahead into scored sessions.
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
        if bar.tick_count <= 0:
            return
        if self._range_high is None or bar.high > self._range_high:
            self._range_high = bar.high
        if self._range_low is None or bar.low < self._range_low:
            self._range_low = bar.low

    def _manage_position(self, ctx) -> None:
        if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
            if ctx.working_order_ids:
                ctx.cancel_all()
            ctx.close()
            self._deadline_ns = None

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
            self._entry_orders_placed = False

        # Once enough live daily bars exist, prefer them over the training seed.
        if len(ctx.history(self.daily_interval)) >= self.trailing_days:
            self._update_daily_vol(ctx)

        rth_open = ctx.session_start + self.rth_open_offset_ns
        range_end = rth_open + self.range_ns

        if bar.close_ts <= rth_open:
            return

        # Accumulate the opening range.  The bar closing exactly at range_end
        # completes the range but is not a decision bar.
        if bar.close_ts <= range_end:
            self._update_range(bar)
            if bar.close_ts == range_end:
                self._range_done = True
            return

        if not self._range_done:
            self._range_done = True

        if not ctx.actionable:
            return

        if ctx.position:
            self._manage_position(ctx)
            return

        if ctx.working_order_ids:
            # Flat but entry orders are still resting; cancel them once the
            # entry window has passed.
            if bar.close_ts > rth_open + self.entry_window_ns:
                ctx.cancel_all()
            return

        if self._traded_this_session or self._entry_orders_placed:
            return

        if bar.close_ts > rth_open + self.entry_window_ns:
            return

        if self._range_high is None or self._range_low is None:
            return
        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        stop_off = max(
            self.stop_mult * buffer,
            self.min_stop_frac * range_width,
            self.min_stop_points,
        )
        target_off = max(self.target_mult * buffer, stop_off * self.reward_risk)
        target_off = max(target_off, stop_off * 1.1)

        trigger_long = self._range_high + buffer
        trigger_short = self._range_low - buffer

        ctx.buy(
            size=self.size,
            type="stop",
            trigger=trigger_long,
            stop_offset=stop_off,
            target_offset=target_off,
            tag="orb-entry-long",
        )
        ctx.sell(
            size=self.size,
            type="stop",
            trigger=trigger_short,
            stop_offset=stop_off,
            target_offset=target_off,
            tag="orb-entry-short",
        )

        self._entry_orders_placed = True

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._deadline_ns = None
            return

        # A filled entry consumes the session's one trade.  Cancel the sibling
        # resting entry order, but not the bracket legs the engine just armed.
        self._traded_this_session = True
        for tag in ("orb-entry-long", "orb-entry-short"):
            for order in ctx.find_orders(tag=tag):
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
