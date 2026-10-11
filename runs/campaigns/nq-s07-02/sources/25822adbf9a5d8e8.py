"""Opening-range fade: does the range win when price leaves it?

This strategy takes the "range wins" side of the campaign question. When a 5m
close leaves the regular-hours opening range by a volatility-scaled buffer, the
strategy fades the departure: it sells short above the range and buys below it.

Definitions are explicit:
  * Opening range = high/low of 5m bars closing inside the first 30 minutes
    after the RTH open (08:30 CT), plus the bar closing exactly at the end.
  * Volatility = average daily high-low range over the last 20 completed daily
    bars, seeded from the training split for the first sessions.
  * Departure = a 5m close beyond the range by `breakout_vol_mult` times that
    trailing daily volatility.
  * Entry = fade in the opposite direction, at most once per session, and only
    during the first `entry_window_minutes` after the RTH open.
  * Exit = engine-armed bracket with stop/target as positive offsets, and a
    time horizon that closes an undecided position after `max_hold_minutes`.
    The time exit keeps undecided departures from silently counting as either
    a range win or a move hold.

The bracket is armed by the engine at the instant the fade fills, so the stop
and target are symmetrical with the same volatility/range scaling as the entry.
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
        rth_open_offset_seconds: int = 55800,   # 17:00 CT -> 08:30 CT next day
        rth_close_offset_seconds: int = 80100,  # 17:00 CT -> 15:15 CT next day
        trailing_days: int = 20,
        breakout_vol_mult: float = 0.05,
        stop_mult: float = 1.6,
        min_stop_frac: float = 0.5,
        target_range_frac: float = 0.8,
        reward_risk: float = 1.0,
        size: int = 1,
        max_hold_minutes: int = 60,
        entry_window_minutes: int = 120,
        close_margin_seconds: int = 300,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.target_range_frac = target_range_frac
        self.reward_risk = reward_risk

        self.range_ns = range_minutes * 60 * NS_PER_SECOND
        self.entry_window_ns = entry_window_minutes * 60 * NS_PER_SECOND
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.close_margin_ns = close_margin_seconds * NS_PER_SECOND
        self.rth_open_offset_ns = rth_open_offset_seconds * NS_PER_SECOND
        self.rth_close_offset_ns = rth_close_offset_seconds * NS_PER_SECOND

        # Seeded in setup() if training data is available.
        self._seed_daily_vol = 30.0

        self._session = None
        self._rth_open_ns = 0
        self._rth_close_ns = 0
        self._range_end_ns = 0

        self._range_high = None
        self._range_low = None
        self._range_done = False
        self._daily_vol = self._seed_daily_vol
        self._vol_updated = False
        self._traded = False
        self._deadline_ns = None
        self._closing = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.daily_interval)

        # Seed volatility from the training split. This is not lookahead: the
        # training split is entirely before the scored split.
        try:
            dailies = ctx.train_bars(self.daily_interval)
            if len(dailies) > 0:
                ranges = [b.high - b.low for b in dailies if b.high > b.low]
                if ranges:
                    self._seed_daily_vol = sum(ranges) / len(ranges)
        except Exception:
            # If anything unexpected happens, keep the conservative fallback.
            pass

        self._daily_vol = self._seed_daily_vol

    def _reset_session(self, ctx) -> None:
        self._session = ctx.session
        self._rth_open_ns = ctx.session_start + self.rth_open_offset_ns
        self._rth_close_ns = ctx.session_start + self.rth_close_offset_ns
        self._range_end_ns = self._rth_open_ns + self.range_ns

        self._range_high = None
        self._range_low = None
        self._range_done = False
        self._daily_vol = self._seed_daily_vol
        self._vol_updated = False
        self._traded = False
        self._deadline_ns = None
        self._closing = False

    def _update_daily_vol(self, ctx) -> None:
        history = ctx.history(self.daily_interval)
        if len(history) < self.trailing_days:
            return
        days = history.last(self.trailing_days)
        ranges = [b.high - b.low for b in days if b.high > b.low]
        if ranges:
            self._daily_vol = sum(ranges) / len(ranges)

    def _update_range(self, bar) -> None:
        if bar.tick_count == 0:
            return
        if self._range_high is None or bar.high > self._range_high:
            self._range_high = bar.high
        if self._range_low is None or bar.low < self._range_low:
            self._range_low = bar.low

    def _flatten(self, ctx) -> None:
        if ctx.working_order_ids:
            ctx.cancel_all()
        ctx.close()
        self._closing = True
        self._deadline_ns = None

    def _manage_open(self, ctx) -> None:
        if self._closing:
            return

        hard_deadline = self._rth_close_ns - self.close_margin_ns

        if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
            self._flatten(ctx)
            return

        if ctx.ts >= hard_deadline:
            self._flatten(ctx)

    def _maybe_enter(self, ctx, bar) -> None:
        if self._traded or bar.tick_count == 0:
            return

        rth_open = self._rth_open_ns
        if bar.close_ts > rth_open + self.entry_window_ns:
            return
        if self._range_high is None or self._range_low is None:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        side = 0
        if bar.close > self._range_high + buffer:
            side = -1  # fade the upside departure
        elif bar.close < self._range_low - buffer:
            side = 1   # fade the downside departure
        if side == 0:
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        stop_off = max(self.stop_mult * buffer, self.min_stop_frac * range_width, 2.0)
        target_off = max(buffer + self.target_range_frac * range_width,
                         stop_off * self.reward_risk)
        target_off = max(target_off, stop_off * 1.05)

        if side > 0:
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-fade-long",
            )
        else:
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-fade-short",
            )

        if oid is not None:
            self._traded = True

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._reset_session(ctx)

        if not self._vol_updated:
            self._update_daily_vol(ctx)
            self._vol_updated = True

        rth_open = self._rth_open_ns
        range_end = self._range_end_ns

        if bar.close_ts <= rth_open:
            return

        # Opening range accumulation. The bar closing exactly at range_end is
        # still part of the range.
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

        if not ctx.actionable:
            return

        if ctx.position:
            self._manage_open(ctx)
            return

        if ctx.working_order_ids:
            return

        self._maybe_enter(ctx, bar)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._closing = False
            self._deadline_ns = None
            return

        if self._deadline_ns is None:
            deadline = ctx.ts + self.max_hold_ns
            hard_deadline = self._rth_close_ns - self.close_margin_ns
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

        self._closing = False

    def on_lifecycle(self, ctx) -> None:
        pass
