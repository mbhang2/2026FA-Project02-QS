"""Opening-range fade: trade the reversion when price leaves the range.

This strategy answers the campaign question with "the range wins": when price
breaks out of the opening range by a volatility-scaled buffer, we take the
opposite side, expecting price to return inside the range.

The structure mirrors the best breakout strategy (#279) but reverses the entry
direction and adjusts the stop/target logic. A fade short is entered when price
breaks above the range high + buffer; a fade long when price breaks below the
range low - buffer. The protective stop is placed beyond the breakout extreme,
and the profit target is set at the range boundary (or a multiple of the buffer)
to capture the reversion.

The delta filter is inverted relative to the breakout version: for a fade short
we require negative delta (selling pressure) on the breakout bar, and for a fade
long we require positive delta. This filters for exhaustion rather than
continuation.

All other parameters (range length, volatility estimation, time exits) are kept
from the best prior trial to isolate the effect of the fade direction.
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
        stop_mult: float = 1.0,                # stop distance as multiple of buffer
        target_mult: float = 1.0,               # target distance as multiple of buffer
        size: int = 1,
        max_hold_minutes: int = 120,
        entry_window_minutes: int = 120,
        hard_deadline_minutes: int = 30,
        max_trades_per_session: int = 2,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
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

        # Manual trailing-stop state. _side is 1 for long, -1 for short,
        # 0 for flat. _peak is the best close seen while long or the lowest
        # close seen while short, depending on the side.
        self._side: int = 0
        self._peak: float | None = None
        self._current_stop: float | None = None
        self._entry_stop_off: float | None = None
        self._entry_target: float | None = None

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

    def _place_stop_and_target(self, ctx, fill_price: float, size: int) -> None:
        """Place a reduce-only stop and a profit target for the fade position."""
        if self._entry_stop_off is None or self._entry_target is None:
            return

        if self._side > 0:  # long fade (price broke down)
            stop_trigger = fill_price - self._entry_stop_off
            target_price = fill_price + self._entry_target
            ctx.sell(
                size=size,
                type="stop",
                trigger=stop_trigger,
                tif="GTC",
                reduce_only=True,
                tag="fade-stop",
            )
            ctx.sell(
                size=size,
                type="limit",
                limit=target_price,
                tif="GTC",
                reduce_only=True,
                tag="fade-target",
            )
        else:  # short fade (price broke up)
            stop_trigger = fill_price + self._entry_stop_off
            target_price = fill_price - self._entry_target
            ctx.buy(
                size=size,
                type="stop",
                trigger=stop_trigger,
                tif="GTC",
                reduce_only=True,
                tag="fade-stop",
            )
            ctx.buy(
                size=size,
                type="limit",
                limit=target_price,
                tif="GTC",
                reduce_only=True,
                tag="fade-target",
            )

        self._current_stop = stop_trigger

    def _trail_stop(self, ctx, bar) -> None:
        """Trail the protective stop behind the best close reached."""
        if self._side == 0 or self._entry_stop_off is None or self._current_stop is None:
            return

        stops = ctx.find_orders(tag="fade-stop")
        if not stops:
            return

        if self._side > 0:
            self._peak = max(self._peak, bar.close)
            new_stop = self._peak - self._entry_stop_off
            if new_stop > self._current_stop:
                ctx.modify(stops[0].id, trigger=new_stop)
                self._current_stop = new_stop
        else:
            self._peak = min(self._peak, bar.close)
            new_stop = self._peak + self._entry_stop_off
            if new_stop < self._current_stop:
                ctx.modify(stops[0].id, trigger=new_stop)
                self._current_stop = new_stop

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
            self._peak = None
            self._current_stop = None
            self._entry_stop_off = None
            self._entry_target = None

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
            else:
                self._trail_stop(ctx, bar)
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
        # Fade logic: breakout up -> short, breakout down -> long.
        if bar.high > self._range_high + buffer:
            side = -1  # short fade
        elif bar.low < self._range_low - buffer:
            side = 1   # long fade
        if side == 0:
            return

        # Delta filter: require order flow opposite to the breakout direction.
        if side < 0 and bar.delta >= 0:  # short fade needs selling pressure
            return
        if side > 0 and bar.delta <= 0:  # long fade needs buying pressure
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        stop_off = max(self.stop_mult * buffer, 2.0)
        target_off = max(self.target_mult * buffer, 2.0)

        self._entry_stop_off = stop_off
        self._entry_target = target_off

        if side > 0:
            ctx.buy(
                size=self.size,
                tag="fade-long",
            )
        else:
            ctx.sell(
                size=self.size,
                tag="fade-short",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position == 0:
            # A position was closed.
            self._deadline_ns = None
            self._side = 0
            self._peak = None
            self._current_stop = None
            self._entry_stop_off = None
            self._entry_target = None
            return

        # Entry fill.
        self._trade_count += 1

        self._side = 1 if fill.position > 0 else -1
        self._peak = fill.price
        self._entry_stop_off = max(self._entry_stop_off or 2.0, 2.0)
        self._entry_target = max(self._entry_target or 2.0, 2.0)

        self._place_stop_and_target(ctx, fill.price, abs(fill.position))

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
