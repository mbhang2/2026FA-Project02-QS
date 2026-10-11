"""Opening-range breakout, one filled entry per session, with 1-minute range.

This build keeps the best-performing structure from prior trials (#0) and
removes the overtrading: after the opening range is complete, the strategy may
fill at most one entry per session.  The opening range is built from 1-minute
bars for exact high/low, while the entry decision is made on 5-minute closes.

Volatility is seeded from the training split so early scored sessions are not
inert, then switches to trailing live daily high-low ranges once enough daily
bars exist.  The departure buffer, stop, and target are the same volatility and
range-scaled quantities as #0, which still has the best completed score in this
campaign.  A time horizon closes an undecided departure before the regular-hours
close.
"""

from __future__ import annotations

from src.core.types import NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_interval: str = "1m",
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
        self.range_interval = range_interval
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

    def setup(self, ctx) -> None:
        # 1m bars carry empty intervals so the opening-range end and the time
        # exit can fire even on a quiet tape.
        ctx.subscribe(self.range_interval, emit_empty=True)
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

        # Seed trailing volatility from the training split.  This is training
        # data only and is entirely before scored sessions, so it carries no
        # lookahead.
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
        # New session: reset state and refresh the volatility estimate once.
        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._deadline_ns = None
            self._traded_this_session = False
            if len(ctx.history(self.daily_interval)) >= self.trailing_days:
                self._update_daily_vol(ctx)
            else:
                self._daily_vol = self._initial_daily_vol

        rth_open = ctx.session_start + self.rth_open_offset_ns
        range_end = rth_open + self.range_ns

        # Update the opening range from the precise 1-minute bars.
        if self.range_interval in ctx.bars:
            rbar = ctx.bars[self.range_interval]
            if rbar.close_ts > rth_open:
                if rbar.close_ts <= range_end:
                    self._update_range(rbar)
                    if rbar.close_ts == range_end:
                        self._range_done = True
                elif not self._range_done:
                    self._range_done = True

        # Position management runs on every callback, including 1m bars, so a
        # time exit is not delayed by waiting for a 5m bar.
        if ctx.position:
            self._manage_position(ctx)
            return

        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if not ctx.actionable:
            return

        if ctx.working_order_ids:
            return

        # One filled entry per session.
        if self._traded_this_session:
            return

        if bar.close_ts <= range_end:
            if bar.close_ts == range_end:
                self._range_done = True
            return

        if not self._range_done:
            self._range_done = True

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

        side = 0
        if bar.close > self._range_high + buffer:
            side = 1
        elif bar.close < self._range_low - buffer:
            side = -1
        if side == 0:
            return

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

        # This was an entry fill; consume the session's one trade.
        self._traded_this_session = True

        if self._deadline_ns is None:
            rth_close = ctx.session_start + self.rth_close_offset_ns
            deadline = ctx.ts + self.max_hold_ns
            hard_deadline = rth_close - 5 * 60 * NS_PER_SECOND
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
