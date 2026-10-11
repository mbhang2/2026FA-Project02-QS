"""Opening-range breakout with resting stop entries and one trade per session.

This strategy asks: when price leaves the regular-hours opening range, does the
move hold or does the range win?  It takes the "move holds" side, entering in the
direction of the first departure.

The departure is defined in volatility-scaled terms:
  * opening range = high/low of the first 30 minutes of the RTH session,
    built from 5m bars
  * volatility = average high-low range of the last 20 completed daily bars,
    seeded from the training split so early scored sessions are tradable
  * departure buffer = 0.04 * trailing daily volatility

The entry mechanism differs from the previous completed build: after the opening
range completes, two resting stop orders are placed at the upper and lower
borders of the range plus the volatility buffer.  This enters on the first
intra-bar trade through the boundary rather than waiting for a subsequent 5m
close confirmation.  When one entry fills, the opposite entry is cancelled.

Only one filled entry is permitted per session.  Earlier completed work traded
483 times over 384 sessions; the extra re-entries after bracket fills paid
commission without a corresponding edge.  The engine-armed bracket supplies the
protective stop and target.  A time horizon closes any position still open after
75 minutes, or five minutes before the RTH close, whichever is earlier, so an
undecided departure is not silently carried overnight or folded into one of the
two accounts.

The 5m subscription uses emit_empty=True so the time exit and end-of-session
defence can fire even when the session has gone quiet.
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

        # Seed volatility from the training split, so the first scored sessions
        # are tradable before 20 live daily bars have accumulated.
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

        # Accumulate the opening range until the exact completing bar.  Empty
        # bars advance time but do not alter the range.
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

        # Past the entry window, any still-working entry stops are stale.
        if bar.close_ts > entry_end:
            for tag in ("orb-entry-long", "orb-entry-short"):
                for order in ctx.find_orders(tag=tag):
                    ctx.cancel(order.id)
            return

        if ctx.working_order_ids:
            return

        if self._entry_fills >= self.max_trades_per_session:
            return

        if self._range_high is None or self._range_low is None:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        buffer = self.breakout_vol_mult * self._daily_vol

        stop_off = max(self.stop_mult * buffer, self.min_stop_frac * range_width, 2.0)
        target_off = max(self.target_mult * buffer, stop_off * self.reward_risk)
        target_off = max(target_off, stop_off * 1.1)

        buy_trigger = self._range_high + buffer
        sell_trigger = self._range_low - buffer

        ctx.buy(
            size=self.size,
            type="stop",
            trigger=buy_trigger,
            stop_offset=stop_off,
            target_offset=target_off,
            tif="DAY",
            tag="orb-entry-long",
        )
        ctx.sell(
            size=self.size,
            type="stop",
            trigger=sell_trigger,
            stop_offset=stop_off,
            target_offset=target_off,
            tif="DAY",
            tag="orb-entry-short",
        )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position == 0:
            self._deadline_ns = None
            return

        # One entry has filled.  Cancel the opposite resting entry, so the two
        # sides cannot both be live after a fast reversal.
        if fill.position > 0:
            for order in ctx.find_orders(tag="orb-entry-short"):
                ctx.cancel(order.id)
        elif fill.position < 0:
            for order in ctx.find_orders(tag="orb-entry-long"):
                ctx.cancel(order.id)

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
