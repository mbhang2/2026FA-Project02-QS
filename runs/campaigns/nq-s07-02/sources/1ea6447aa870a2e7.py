"""Opening-range breakout with a bracket exit: stop and target, plus a time horizon.

The strategy asks the campaign question: when price leaves the regular-hours
opening range, does the move hold or does the range win?  It trades the
departure direction (long above, short below), but only when the breakout bar's
signed order flow (delta) agrees with the direction.

Range: first 30 minutes of the regular-hours session, built from 5m bars.
Volatility: average daily high-low over the last 20 completed sessions, seeded
from the training split so early scored sessions are not inert.

Departure: a 5m close beyond the range by a fraction of trailing daily
volatility.

Filter: the breakout bar's delta (buy_volume - sell_volume) must be positive
for a long and negative for a short.  This removes breakouts that occur on
weak or counter-directional order flow, which are more likely to be false
departures that return inside the range.

Exit: a bracket with a protective stop and a profit target, both given as
offsets from the fill price.  The stop is placed at a distance based on
volatility and the opening range width, and the target is placed further away
to give the trade a positive risk-reward.  A time horizon flattens any position
that has not been decided by then, so undecided departures are counted
separately rather than folded into either account.

This build replaces the manual trailing stop of the best prior trial (#39)
with a fixed bracket.  The hypothesis is that a fixed target locks in profits
more reliably than a trailing stop, and the bracket is armed by the engine at
the instant of the fill, eliminating the small gap between entry and stop
placement.  All other parameters are kept from #39, with the addition of
target_mult and target_frac to define the target distance.
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
        target_mult: float = 4.0,
        target_frac: float = 1.5,
        min_target: float = 4.0,
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
        self.target_mult = target_mult
        self.target_frac = target_frac
        self.min_target = min_target
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

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._daily_vol = self._initial_daily_vol
            self._deadline_ns = None
            self._trade_count = 0

        self._update_daily_vol(ctx)

        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Manage an open position first, including on empty bars, so the
        # time horizon can exit even in a quiet session.
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
        target_off = max(
            self.target_mult * buffer,
            self.target_frac * range_width,
            self.min_target,
        )

        if side > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-entry-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-entry-short",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position == 0:
            # A position was closed (by stop, target, or manual close).
            self._deadline_ns = None
            return

        # Entry fill.
        self._trade_count += 1

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
