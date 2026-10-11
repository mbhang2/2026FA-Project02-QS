"""Opening-range breakout with a close-based trailing stop, a delta filter,
and a genuine wide-trail ratchet for confirmed moves.

This build starts from the best prior positive trial (#39) and keeps its
entry, initial-stop, and horizon parameters unchanged: a 30-minute
regular-hours opening range, breakout distance 0.06 of trailing 20-day daily
range, a delta filter on the breakout bar, a close-based trailing stop, a
120-minute time horizon, and at most two trades per session.

The one change is on the exit side.  Prior trials that *tightened* the trail
after a profitable move (#66, #67) made the score worse.  This version does
the opposite: once a trade has run 2.0 initial risk units beyond the entry,
the trailing distance becomes 1.5 times the initial stop distance.

The important implementation detail is that the base trailing logic only
moves a stop in the favourable direction.  A wider distance is, by design,
*less* favourable than the tight trail that has been ratcheting up to that
point, so the old one-way ratchet would never actually adopt it except on a
single-bar gap.  Here a `_wide_trail_active` flag is set when the move
threshold is reached, and from then on the stop is allowed to re-position to
the wide-trail level even if that is initially a step away from price.  This
gives confirmed movers room to breathe through normal pullbacks and is aimed
at preserving the long right tail that carries the strategy's P&L.

All other parameters remain at their #39 values.
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
        stop_mult: float = 3.0,
        min_stop_frac: float = 0.75,
        size: int = 1,
        max_hold_minutes: int = 120,
        entry_window_minutes: int = 120,
        hard_deadline_minutes: int = 5,
        max_trades_per_session: int = 2,
        wide_trail_trigger_r: float = 2.0,
        wide_trail_mult: float = 1.5,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.max_trades_per_session = max_trades_per_session
        self.wide_trail_trigger_r = wide_trail_trigger_r
        self.wide_trail_mult = wide_trail_mult

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

        # Manual trailing-stop state.  _side is 1 for long, -1 for short,
        # 0 for flat.  _peak is the best *close* seen while long or the
        # lowest *close* seen while short, depending on the side.
        self._side: int = 0
        self._peak: float | None = None
        self._current_stop: float | None = None
        self._entry_stop_off: float | None = None
        self._entry_price: float | None = None
        self._wide_trail_active: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

        # Seed the volatility estimate from the training split. This makes
        # the first scored sessions tradable while live daily bars accumulate.
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
            # Use whatever completed daily bars exist if we have no seed yet.
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
        """Trail the protective stop behind the best close reached.

        Before the wide-trail threshold is reached, the stop ratchets in the
        favourable direction only, exactly as in #39.  Once the trade has run
        `wide_trail_trigger_r` initial risk units beyond entry, the trail
        distance becomes `wide_trail_mult` times the initial stop distance.
        Because that widening is a step *away* from price, the method tracks
        the flag and allows that one less-favourable repositioning, then
        continues ratcheting with the wider distance.
        """
        if self._side == 0 or self._entry_stop_off is None or self._current_stop is None:
            return

        stops = ctx.find_orders(tag="orb-stop")
        if not stops:
            return

        if self._side > 0:
            self._peak = max(self._peak, bar.close)

            if self._entry_price is not None and not self._wide_trail_active:
                best_move = self._peak - self._entry_price
                if best_move >= self.wide_trail_trigger_r * self._entry_stop_off:
                    self._wide_trail_active = True

            trail_off = self._entry_stop_off
            if self._wide_trail_active:
                trail_off = self._entry_stop_off * self.wide_trail_mult

            new_stop = self._peak - trail_off

            if self._wide_trail_active:
                if new_stop != self._current_stop:
                    ctx.modify(stops[0].id, trigger=new_stop)
                    self._current_stop = new_stop
            elif new_stop > self._current_stop:
                ctx.modify(stops[0].id, trigger=new_stop)
                self._current_stop = new_stop
        else:
            self._peak = min(self._peak, bar.close)

            if self._entry_price is not None and not self._wide_trail_active:
                best_move = self._entry_price - self._peak
                if best_move >= self.wide_trail_trigger_r * self._entry_stop_off:
                    self._wide_trail_active = True

            trail_off = self._entry_stop_off
            if self._wide_trail_active:
                trail_off = self._entry_stop_off * self.wide_trail_mult

            new_stop = self._peak + trail_off

            if self._wide_trail_active:
                if new_stop != self._current_stop:
                    ctx.modify(stops[0].id, trigger=new_stop)
                    self._current_stop = new_stop
            elif new_stop < self._current_stop:
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
            self._entry_price = None
            self._wide_trail_active = False

        self._update_daily_vol(ctx)

        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Manage an open position first, including on empty bars, so the
        # trailing stop can fire and the time horizon can exit even in a quiet
        # session.
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
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        side = 0
        if bar.close > self._range_high + buffer:
            side = 1
        elif bar.close < self._range_low - buffer:
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
            # A position was closed.
            self._deadline_ns = None
            self._side = 0
            self._peak = None
            self._current_stop = None
            self._entry_stop_off = None
            self._entry_price = None
            self._wide_trail_active = False
            return

        # Entry fill.
        self._trade_count += 1

        self._side = 1 if fill.position > 0 else -1
        self._peak = fill.price
        self._entry_price = fill.price
        self._entry_stop_off = max(self._entry_stop_off or 2.0, 2.0)
        self._wide_trail_active = False

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
