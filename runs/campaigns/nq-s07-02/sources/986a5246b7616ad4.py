"""Opening-range breakout with close-based trailing stop, delta filter, and
asymmetric time extension for winners.

This build starts from the best prior trial (#158, score +0.1370) and changes
only the time-horizon rule.  #158 kept all positions for at most 120 minutes.
#222 tried cutting underwater positions at 90 minutes and was worse, which
suggests that cutting losers early is the wrong side of the asymmetry.

Here the loser rule is left exactly as #158: every position is given a 120
minute base horizon.  A position that is profitable when that base horizon is
reached is extended to 180 minutes before the time exit is applied.  The
trailing stop remains active throughout, so the extension only allows winning
breakouts more time to run; losing/undecided positions still exit at the
original 120 minute boundary.  This keeps the "move holds" side of the
campaign question from being prematurely closed while preserving the #158
behaviour for "range wins".

All other parameters are unchanged from #158:
range_minutes=30, breakout_vol_mult=0.06,
stop_mult=3.625, min_stop_frac=0.75,
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
        max_hold_minutes: int = 120,            # unchanged from #158
        winner_hold_minutes: int = 180,         # new: extend profitable trades
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
        self.winner_hold_ns = winner_hold_minutes * 60 * NS_PER_SECOND
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

        # Manual trailing-stop state.
        self._side: int = 0
        self._peak: float | None = None
        self._current_stop: float | None = None
        self._entry_stop_off: float | None = None

        # Asymmetric time-extension state.
        self._entry_ts: int | None = None
        self._winner_extended: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

        # Seed volatility from the training split so the first scored sessions
        # are tradable before enough live daily bars accumulate.
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
        """Place a reduce-only stop for the freshly filled position."""
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
        """Trail the protective stop behind the best close reached."""
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
            self._entry_ts = None
            self._winner_extended = False

        self._update_daily_vol(ctx)

        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Manage an open position first, including on empty bars.
        if ctx.position:
            if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
                rth_close = min(
                    ctx.session_start + self.rth_close_offset_ns,
                    ctx.session_end,
                )
                hard_deadline = rth_close - self.hard_deadline_ns

                # Asymmetric extension: a profitable trade gets one extension
                # from the base horizon to the winner horizon; losing and
                # undecided trades keep the original #158 time exit.
                if (
                    not self._winner_extended
                    and self._entry_ts is not None
                    and ctx.unrealized_pnl > 0
                    and ctx.ts < hard_deadline
                ):
                    self._deadline_ns = min(
                        self._entry_ts + self.winner_hold_ns,
                        hard_deadline,
                    )
                    self._winner_extended = True
                    self._trail_stop(ctx, bar)
                else:
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

        # Accumulate the opening range.  The exact closing bar completes the
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
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        side = 0
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

        range_width = max(self._range_high - self._range_low, 0.0)
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
            self._entry_ts = None
            self._winner_extended = False
            return

        # Entry fill.
        self._trade_count += 1

        self._side = 1 if fill.position > 0 else -1
        self._peak = fill.price
        self._entry_stop_off = max(self._entry_stop_off or 2.0, 2.0)
        self._entry_ts = ctx.ts
        self._winner_extended = False

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
