"""Opening-range fade: when price leaves the range, bet that the range wins.

This strategy tests the opposite hypothesis from the breakout approach: that
departures from the opening range are more often false and price returns
inside.  When a 5m bar closes beyond the range by a volatility-scaled buffer,
we enter in the opposite direction (fade the breakout).  A protective stop is
placed just beyond the breakout bar's extreme, and a profit target is set at
the midpoint of the opening range.  A time horizon flattens any position that
has not been decided, so undecided departures are counted separately.

Range: first 30 minutes of the regular-hours session, built from 5m bars.
Volatility: average daily high-low over the last 20 completed sessions, seeded
from the training split.

Entry: a 5m close above range_high + buffer triggers a short; a close below
range_low - buffer triggers a long.  No delta filter is applied -- we fade all
breakouts, as the hypothesis is that the range itself is the attractor.

Exit: fixed stop beyond the breakout bar's high (for shorts) or low (for
longs), and a target at the range midpoint.  Both are placed as reduce-only
orders immediately after entry.  A 120-minute maximum holding period flattens
any remaining position.

This is a new direction for the campaign, directly testing the "range wins"
account.
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

        # State for the open fade position.
        self._side: int = 0          # 1 long, -1 short, 0 flat
        self._entry_price: float | None = None
        self._stop_price: float | None = None
        self._target_price: float | None = None
        self._breakout_high: float | None = None
        self._breakout_low: float | None = None

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
        if not len(hist):
            return

        if len(hist) < self.trailing_days:
            if self._daily_vol is None:
                ranges = [b.high - b.low for b in hist if b.high > b.low]
                if ranges:
                    self._daily_vol = sum(ranges) / len(ranges)
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

    def _place_stop_and_target(self, ctx, fill_price: float, size: int) -> None:
        """Place reduce-only stop and target for the fade position."""
        # Guard against None values that mypy cannot infer are set.
        if self._range_high is None or self._range_low is None:
            return
        if self._breakout_low is None or self._breakout_high is None:
            return
        if self._side == 0:
            return

        if self._side > 0:  # long fade (betting on reversal upward)
            # Stop below the breakout bar's low.
            stop_trigger = self._breakout_low - 0.25  # small buffer
            # Target at range midpoint.
            target_price = (self._range_high + self._range_low) / 2.0
            # Ensure target is above entry (for long).
            if target_price <= fill_price:
                target_price = fill_price + 0.5  # fallback
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
        else:  # short fade
            stop_trigger = self._breakout_high + 0.25
            target_price = (self._range_high + self._range_low) / 2.0
            if target_price >= fill_price:
                target_price = fill_price - 0.5
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

        self._stop_price = stop_trigger
        self._target_price = target_price

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
            self._entry_price = None
            self._stop_price = None
            self._target_price = None
            self._breakout_high = None
            self._breakout_low = None

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
        if bar.close > self._range_high + buffer:
            side = -1  # fade the upside breakout -> short
        elif bar.close < self._range_low - buffer:
            side = 1   # fade the downside breakout -> long
        if side == 0:
            return

        # Store breakout bar extremes for stop placement.
        self._breakout_high = bar.high
        self._breakout_low = bar.low

        if side > 0:
            ctx.buy(size=self.size, tag="fade-entry-long")
        else:
            ctx.sell(size=self.size, tag="fade-entry-short")

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position == 0:
            # Position closed (by stop, target, or time exit).
            self._deadline_ns = None
            self._side = 0
            self._entry_price = None
            self._stop_price = None
            self._target_price = None
            self._breakout_high = None
            self._breakout_low = None
            return

        # Entry fill.
        self._trade_count += 1
        self._side = 1 if fill.position > 0 else -1
        self._entry_price = fill.price

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
