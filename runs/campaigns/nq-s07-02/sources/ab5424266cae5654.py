"""Opening-range fade: when price departs the regular-hours opening range,
this strategy trades the return to the range.

The campaign asks whether the move holds or the range wins.  Prior builds in
this campaign traded the direction of the departure ("move holds") and produced
at best a marginally positive score.  This build takes the other stated
answer -- the range wins -- so a 5m close above the opening range by a
volatility-scaled buffer is sold, and a close below it is bought.

Range: first 30 minutes of the regular-hours session, built from 5m bars.
Volatility: average daily high-low over the last 20 completed sessions, seeded
from the training split.  Stop: the larger of a 2.5x volatility buffer and half
the opening-range width, with a 2.0-point floor.  Target: the midpoint of the
opening range.  A position still open after 75 minutes, or five minutes before
the regular-hours close, is flattened, so an undecided departure is neither a
range win nor a move hold.
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
        breakout_vol_mult: float = 0.04,
        stop_mult: float = 2.5,
        min_stop_frac: float = 0.5,
        min_stop_points: float = 2.0,
        size: int = 1,
        max_hold_minutes: int = 75,
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
        self.min_stop_points = min_stop_points
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

        # Seed the volatility estimate from the training split.  This is
        # entirely before scored sessions, so it carries no lookahead.
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
            # If no seed exists yet, use whatever completed daily bars exist.
            # This matters mainly for warmup and for very short synthetic runs.
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
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._daily_vol = self._initial_daily_vol
            self._deadline_ns = None
            self._trade_count = 0

        self._update_daily_vol(ctx)

        # Manage an open position first, including on empty bars, so the time
        # horizon can fire even when the session has gone quiet.
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
        range_end = rth_open + self.range_ns

        if bar.close_ts <= rth_open:
            return

        # Accumulate the opening range.  A bar closing exactly at range_end is
        # part of the range, so no decision is made on it yet.
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
        if bar.close_ts > rth_open + self.entry_window_ns:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        side = 0
        if bar.close > self._range_high + buffer:
            side = -1          # fade a high breakout: sell
        elif bar.close < self._range_low - buffer:
            side = 1           # fade a low breakout: buy
        if side == 0:
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        stop_off = max(
            self.stop_mult * buffer,
            self.min_stop_frac * range_width,
            self.min_stop_points,
        )
        mid = (self._range_high + self._range_low) / 2.0
        ref = ctx.last_price

        if side > 0:
            stop_price = ref - stop_off
            target_price = mid
            if target_price <= ref:
                # The bracket would be rejected if the target is not above the
                # entry; skip a malformed setup rather than crash.
                return
            ctx.buy(
                size=self.size,
                stop=stop_price,
                target=target_price,
                tag="orb-fade-long",
            )
        else:
            stop_price = ref + stop_off
            target_price = mid
            if target_price >= ref:
                return
            ctx.sell(
                size=self.size,
                stop=stop_price,
                target=target_price,
                tag="orb-fade-short",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._deadline_ns = None
            return

        self._trade_count += 1

        if self._deadline_ns is None:
            rth_close = ctx.session_start + self.rth_close_offset_ns
            deadline = ctx.ts + self.max_hold_ns
            hard_deadline = rth_close - self.hard_deadline_ns
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
