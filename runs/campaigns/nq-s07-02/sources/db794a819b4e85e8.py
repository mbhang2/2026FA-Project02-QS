"""Opening-range fade: bet that price returns inside the range after a breakout.

This strategy tests the "range wins" hypothesis: when price leaves the opening
range by a volatility-scaled buffer, we enter in the opposite direction,
expecting a reversion back into the range. The exit is a bracket with a stop
beyond the breakout and a target inside the range, plus a time-based exit for
undecided departures.

Parameters are adapted from the best breakout trial (#158) but with entry
direction reversed and a target added. The delta filter is inverted: we fade
when the breakout bar shows strong order flow in the breakout direction,
treating it as potential exhaustion.

All other mechanics (range accumulation, volatility scaling, session timing)
are kept from the breakout strategy.
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
        target_mult: float = 1.0,
        target_frac: float = 0.5,
        size: int = 1,
        max_hold_minutes: int = 120,
        entry_window_minutes: int = 120,
        hard_deadline_minutes: int = 5,
        max_trades_per_session: int = 2,
        range_width_max_mult: float = 0.0,  # 0 disables the filter
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
        self.max_trades_per_session = max_trades_per_session
        self.range_width_max_mult = range_width_max_mult

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

        # Manage an open position: time-based exit for undecided departures.
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
        # Detect breakout: price exceeds range high/low by buffer.
        if bar.high > self._range_high + buffer:
            side = -1  # fade short (sell) after upward breakout
        elif bar.low < self._range_low - buffer:
            side = 1   # fade long (buy) after downward breakout
        if side == 0:
            return

        # Inverted delta filter: fade when breakout bar shows strong order flow
        # in the breakout direction (potential exhaustion).
        if side == -1 and bar.delta <= 0:
            return
        if side == 1 and bar.delta >= 0:
            return

        range_width = max(self._range_high - self._range_low, 0.0)

        # Optional range-width filter (disabled by default).
        if self.range_width_max_mult > 0 and range_width > self.range_width_max_mult * self._daily_vol:
            return

        # Compute stop and target offsets.
        stop_off = max(
            self.stop_mult * buffer,
            self.min_stop_frac * range_width,
            2.0,
        )
        target_off = max(
            self.target_mult * buffer,
            self.target_frac * range_width,
            2.0,
        )

        if side == -1:
            # Fade short: sell with stop above and target below.
            ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-fade-short",
            )
        else:
            # Fade long: buy with stop below and target above.
            ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-fade-long",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position == 0:
            # Position closed (by stop, target, or time exit).
            self._deadline_ns = None
            return

        # Entry fill: set the time horizon for undecided departures.
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
