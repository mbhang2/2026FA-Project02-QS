"""Opening-range breakout from #158 with conditional extension of protected winners.

#158 scores +0.1370 with max_hold_minutes=120 and a close-based trailing stop.
That forces *every* open position to exit at the 120-minute deadline, including
trades that are already profitable and have their trailing stop at or beyond the
entry price.

This build keeps all #158 entry and stop parameters unchanged and changes only
the deadline rule. When the 120-minute deadline arrives:

- If the position is profitable and the trailing stop already protects the
  entry price, the trade is allowed to continue until the hard intraday
  deadline, protected by the trailing stop.
- Otherwise the position is closed at the deadline exactly as #158 did.

This separates "undecided but protected" departures from "undecided and
unprotected" departures. The former are allowed to resolve; the latter are
still cut.

A small fallback buffer is added for the rare case where no daily volatility
estimate has been seeded yet: the opening range width itself is used as the
breakout buffer. On the real NQ data, `setup()` seeds the volatility estimate
from the training split, so this fallback normally never activates. It exists so
the first sessions of a warm run are still tradable rather than silently flat.

All other parameters are identical to #158:
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
        extend_protected_winners: bool = True,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.max_trades_per_session = max_trades_per_session
        self.extend_protected_winners = extend_protected_winners

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
        self._entry_fill_price: float | None = None
        self._extended = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

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
        if self._entry_stop_off is None:
            return

        if self._side > 0:
            trigger = fill_price - self._entry_stop_off
            ctx.sell(
                size=size,
                type="stop",
                trigger=trigger,
                tif="GTC",
                reduce_only=True,
                tag="orb-stop",
            )
        else:
            trigger = fill_price + self._entry_stop_off
            ctx.buy(
                size=size,
                type="stop",
                trigger=trigger,
                tif="GTC",
                reduce_only=True,
                tag="orb-stop",
            )

        self._current_stop = trigger

    def _trail_stop(self, ctx, bar) -> None:
        if self._side == 0 or self._entry_stop_off is None or self._current_stop is None:
            return

        stops = ctx.find_orders(tag="orb-stop")
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

    def _protected_by_stop(self) -> bool:
        if self._side == 0 or self._current_stop is None or self._entry_fill_price is None:
            return False
        if self._side > 0:
            return self._current_stop >= self._entry_fill_price
        return self._current_stop <= self._entry_fill_price

    def _hard_deadline_ts(self, ctx) -> int:
        rth_close = min(
            ctx.session_start + self.rth_close_offset_ns,
            ctx.session_end,
        )
        return max(ctx.ts, rth_close - self.hard_deadline_ns)

    def _close_at_deadline(self, ctx) -> None:
        if ctx.working_order_ids:
            ctx.cancel_all()
        ctx.close()
        self._deadline_ns = None
        self._extended = False

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
            self._entry_fill_price = None
            self._extended = False

        self._update_daily_vol(ctx)

        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Manage an open position first. Trail using the current bar before any
        # deadline decision so an extension check sees the most recent stop.
        if ctx.position:
            self._trail_stop(ctx, bar)

            if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
                if self._extended:
                    self._close_at_deadline(ctx)
                elif (
                    self.extend_protected_winners
                    and ctx.unrealized_pnl > 0
                    and self._protected_by_stop()
                ):
                    self._deadline_ns = self._hard_deadline_ts(ctx)
                    self._extended = True
                else:
                    self._close_at_deadline(ctx)
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

        # Accumulate the opening range. The exact closing bar completes the
        # range but its close is still inside it, so it is not a decision bar.
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

        range_width = max(self._range_high - self._range_low, 0.0)
        if range_width <= 0:
            return

        # Use the daily volatility estimate for the breakout buffer. If no
        # estimate exists yet, fall back to the observed opening range width.
        if self._daily_vol is None or self._daily_vol <= 0:
            buffer = 0.5 * range_width
        else:
            buffer = self.breakout_vol_mult * self._daily_vol

        side = 0
        # Use high/low instead of close for breakout detection.
        if bar.high > self._range_high + buffer:
            side = 1
        elif bar.low < self._range_low - buffer:
            side = -1
        if side == 0:
            return

        # Delta filter: require order flow in the direction of the breakout.
        if side > 0 and bar.delta <= 0:
            return
        if side < 0 and bar.delta >= 0:
            return

        stop_off = max(
            self.stop_mult * buffer,
            self.min_stop_frac * range_width,
            2.0,
        )

        self._entry_stop_off = stop_off

        if side > 0:
            ctx.buy(
                size=self.size,
                tag="orb-entry-long",
            )
        else:
            ctx.sell(
                size=self.size,
                tag="orb-entry-short",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position == 0:
            self._deadline_ns = None
            self._side = 0
            self._peak = None
            self._current_stop = None
            self._entry_stop_off = None
            self._entry_fill_price = None
            self._extended = False
            return

        # Entry fill.
        self._trade_count += 1

        self._side = 1 if fill.position > 0 else -1
        self._peak = fill.price
        self._entry_fill_price = fill.price
        self._entry_stop_off = max(self._entry_stop_off or 2.0, 2.0)
        self._extended = False

        self._place_stop(ctx, fill.price, abs(fill.position))

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
