"""Opening-range fade: trade the range winning, not the move holding.

This strategy tests the opposite account from the breakout-following builds.
When price leaves the opening range by a volatility-scaled buffer, we fade the
move: short an upside breakout, long a downside breakout. The exit target is
the range boundary (the level price broke through), and a protective stop is
placed beyond the breakout extreme. A delta filter requires order flow against
the breakout direction, suggesting exhaustion.

Parameters are adapted from the best breakout build (#158) but with the side
flipped and the target set to the range boundary. The stop is placed a multiple
of the buffer beyond the breakout price, and a time-based exit caps holding
period.

range_minutes=30, breakout_vol_mult=0.06,
stop_mult=3.625, min_stop_frac=0.75,
entry_window_minutes=120, hard_deadline_minutes=5,
max_trades_per_session=2, trailing_days=20,
max_hold_minutes=120.
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
        breakout_vol_mult: float = 0.06,
        stop_mult: float = 3.625,
        min_stop_frac: float = 0.75,
        size: int = 1,
        max_hold_minutes: int = 120,
        entry_window_minutes: int = 120,
        hard_deadline_minutes: int = 5,
        max_trades_per_session: int = 2,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.max_trades_per_session = max_trades_per_session

        self.range_ns = range_minutes * 60 * NS_PER_SECOND
        self.entry_window_ns = entry_window_minutes * 60 * NS_PER_SECOND
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.hard_deadline_ns = hard_deadline_minutes * 60 * NS_PER_SECOND
        self.rth_open_offset_ns = rth_open_offset_seconds * NS_PER_SECOND
        self.rth_close_offset_ns = rth_close_offset_seconds * NS_PER_SECOND

        self._session = None
        self._range_high = None
        self._range_low = None
        self._range_done = False
        self._daily_vol = None
        self._initial_daily_vol = None
        self._deadline_ns = None
        self._trade_count = 0

        # Manual stop state. _side is 1 for long, -1 for short, 0 for flat.
        self._side: int = 0
        self._entry_stop_off: float | None = None
        self._current_stop: float | None = None
        self._entry_range_width: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

        # Seed the volatility estimate from the training split.
        self._initial_daily_vol = None
        try:
            daily_bars = ctx.train_bars(self.daily_interval)
            if daily_bars:
                recent = daily_bars[-self.trailing_days:]
                ranges = [b.high - b.low for b in recent if b.high > b.low]
                if ranges:
                    self._initial_daily_vol = sum(ranges) / len(ranges)
        except Exception:
            pass

    def _update_daily_vol(self, ctx) -> None:
        hist = ctx.history(self.daily_interval)
        if len(hist):
            if len(hist) < self.trailing_days:
                if self._daily_vol is None:
                    ranges = [b.high - b.low for b in hist if b.high > b.low]
                    if ranges:
                        self._daily_vol = sum(ranges) / len(ranges)
            else:
                days = hist.last(self.trailing_days)
                ranges = [b.high - b.low for b in days if b.high > b.low]
                if ranges:
                    self._daily_vol = sum(ranges) / len(ranges)

        if self._daily_vol is None:
            intraday = ctx.history(self.interval)
            n = min(len(intraday), 20)
            if n >= 2:
                recent = intraday.last(n)
                ranges = [b.high - b.low for b in recent if b.high > b.low]
                if ranges:
                    self._daily_vol = sum(ranges) / len(ranges)

    def _update_range(self, bar) -> None:
        if self._range_high is None or bar.high > self._range_high:
            self._range_high = bar.high
        if self._range_low is None or bar.low < self._range_low:
            self._range_low = bar.low

    def _place_stop(self, ctx, fill_price: float, size: int) -> None:
        """Place a reduce-only stop beyond the breakout extreme."""
        if self._entry_stop_off is None:
            return

        if self._side > 0:  # long fade (price broke down, we bought)
            trigger = fill_price - self._entry_stop_off
            ctx.sell(
                size=size,
                type="stop",
                trigger=trigger,
                tif="GTC",
                reduce_only=True,
                tag="fade-stop",
            )
        else:  # short fade (price broke up, we sold)
            trigger = fill_price + self._entry_stop_off
            ctx.buy(
                size=size,
                type="stop",
                trigger=trigger,
                tif="GTC",
                reduce_only=True,
                tag="fade-stop",
            )
        self._current_stop = trigger

    def _place_target(self, ctx, fill_price: float, size: int) -> None:
        """Place a reduce-only limit target at the range boundary."""
        if self._entry_range_width is None or self._entry_range_width <= 0:
            return

        if self._side > 0:  # long fade: target is range low
            target_price = self._range_low
            ctx.sell(
                size=size,
                type="limit",
                limit=target_price,
                tif="GTC",
                reduce_only=True,
                tag="fade-target",
            )
        else:  # short fade: target is range high
            target_price = self._range_high
            ctx.buy(
                size=size,
                type="limit",
                limit=target_price,
                tif="GTC",
                reduce_only=True,
                tag="fade-target",
            )

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._daily_vol = self._initial_daily_vol
            self._deadline_ns = None
            self._trade_count = 0
            self._side = 0
            self._entry_stop_off = None
            self._current_stop = None
            self._entry_range_width = None

        self._update_daily_vol(ctx)

        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Manage an open position first.
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

        if self._trade_count >= self.max_trades_per_session:
            return

        rth_open = ctx.session_start + self.rth_open_offset_ns
        rth_close = min(
            ctx.session_start + self.rth_close_offset_ns,
            ctx.session_end,
        )
        range_end = rth_open + self.range_ns

        if bar.close_ts <= rth_open:
            return

        # Accumulate the opening range.
        if bar.close_ts < range_end:
            self._update_range(bar)
            return
        if bar.close_ts == range_end:
            self._update_range(bar)
            self._range_done = True
            return
        if not self._range_done:
            self._range_done = True
            return

        if self._range_high is None or self._range_low is None:
            return

        entry_cutoff = min(
            rth_open + self.entry_window_ns,
            rth_close - self.hard_deadline_ns,
        )
        if bar.close_ts > entry_cutoff:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        side = 0
        # Fade: short on upside breakout, long on downside breakout.
        if bar.high > self._range_high + buffer:
            side = -1  # short
        elif bar.low < self._range_low - buffer:
            side = 1   # long
        if side == 0:
            return

        # Delta filter: require order flow against the breakout direction.
        if side < 0 and bar.delta >= 0:  # short fade needs selling pressure
            return
        if side > 0 and bar.delta <= 0:  # long fade needs buying pressure
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        stop_off = max(
            self.stop_mult * buffer,
            self.min_stop_frac * range_width,
            2.0,
        )

        self._entry_stop_off = stop_off
        self._entry_range_width = range_width

        if side > 0:
            ctx.buy(
                size=self.size,
                tag="fade-entry-long",
            )
        else:
            ctx.sell(
                size=self.size,
                tag="fade-entry-short",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position == 0:
            # Position closed (by stop, target, or time exit).
            if ctx.working_order_ids:
                ctx.cancel_all()
            self._deadline_ns = None
            self._side = 0
            self._entry_stop_off = None
            self._current_stop = None
            self._entry_range_width = None
            return

        # Entry fill.
        self._trade_count += 1
        self._side = 1 if fill.position > 0 else -1
        self._entry_stop_off = max(self._entry_stop_off or 2.0, 2.0)

        self._place_stop(ctx, fill.price, abs(fill.position))
        self._place_target(ctx, fill.price, abs(fill.position))

        if self._deadline_ns is None:
            rth_close = min(
                ctx.session_start + self.rth_close_offset_ns,
                ctx.session_end,
            )
            hard_deadline = rth_close - self.hard_deadline_ns
            deadline = ctx.ts + self.max_hold_ns

            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            elif hard_deadline <= ctx.ts:
                deadline = ctx.ts

            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
