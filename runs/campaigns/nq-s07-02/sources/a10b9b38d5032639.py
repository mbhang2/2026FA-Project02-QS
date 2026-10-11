"""Opening-range breakout with range-based stop and range re-entry exit.

This strategy modifies the best prior trial (#151, score +0.1281) by changing
the exit logic to directly test the campaign question: when price leaves the
opening range, does the move hold or does the range win?

Instead of a trailing stop based on a fixed distance from entry, we place a
protective stop just outside the opposite side of the opening range. For a
long, the stop is at range_low - buffer; for a short, at range_high + buffer.
This means the stop is only hit if price moves all the way back through the
range, which is a strong signal that the range has won.

Additionally, we add a rule: if price closes back inside the opening range
after a breakout, we exit immediately with a market order. This captures the
"range wins" outcome without waiting for the stop to be hit, which may be too
far away.

The time-based exit (max_hold) is kept but reduced to 60 minutes to avoid
holding undecided departures for too long.

All other parameters from #151 are retained: range_minutes=30,
breakout_vol_mult=0.06, stop_mult=3.625, min_stop_frac=0.75,
entry_window_minutes=120, hard_deadline_minutes=5,
max_trades_per_session=2, trailing_days=20.
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
        max_hold_minutes: int = 60,  # reduced from 90
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

        # Position state: 1 for long, -1 for short, 0 for flat.
        self._side: int = 0

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

    def _place_range_stop(self, ctx, fill_price: float, size: int) -> None:
        """Place a reduce-only stop just outside the opposite range boundary."""
        if self._range_high is None or self._range_low is None:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        if self._side > 0:
            # Long: stop below range low minus buffer
            trigger = self._range_low - buffer
            ctx.sell(
                size=size,
                type="stop",
                trigger=trigger,
                tif="GTC",
                reduce_only=True,
                tag="orb-range-stop",
            )
        else:
            # Short: stop above range high plus buffer
            trigger = self._range_high + buffer
            ctx.buy(
                size=size,
                type="stop",
                trigger=trigger,
                tif="GTC",
                reduce_only=True,
                tag="orb-range-stop",
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

        self._update_daily_vol(ctx)

        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Manage an open position.
        if ctx.position:
            # Time-based exit
            if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
                if ctx.working_order_ids:
                    ctx.cancel_all()
                ctx.close()
                self._deadline_ns = None
                return

            # Range re-entry exit: if price closes back inside the range,
            # exit immediately.
            if self._side > 0 and bar.close < self._range_high:
                if ctx.working_order_ids:
                    ctx.cancel_all()
                ctx.close()
                return
            if self._side < 0 and bar.close > self._range_low:
                if ctx.working_order_ids:
                    ctx.cancel_all()
                ctx.close()
                return

            # No other management needed; the stop order handles the rest.
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
        # Use high/low for breakout detection.
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

        # No need to compute stop distance; the stop is based on range.
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
            # A position was closed.
            self._deadline_ns = None
            self._side = 0
            return

        # Entry fill.
        self._trade_count += 1
        self._side = 1 if fill.position > 0 else -1

        # Place the range-based protective stop.
        self._place_range_stop(ctx, fill.price, abs(fill.position))

        # Set the time deadline.
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
