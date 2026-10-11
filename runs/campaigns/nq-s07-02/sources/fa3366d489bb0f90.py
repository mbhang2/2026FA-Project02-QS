"""Opening-range fade: when price leaves the range, the range wins.

This is the opposite answer to the breakout attempt. Instead of buying strength
above the opening range and selling weakness below it, this strategy fades the
departure: it sells the first close above the opening range plus a
volatility-scaled buffer and buys the first close below it minus the buffer.

Range: first 30 minutes of the regular-hours session, built from 5m bars.
Volatility: average daily high-low over the last 20 completed sessions.
Departure: a 5m close beyond the range by a fraction of trailing daily
volatility.

Only one trade is taken per session. The bracket is armed by the engine on
fill. The stop is scaled from the same volatility and from the width of the
opening range. The target is the midpoint of the opening range, or at least
1.5x the stop distance. A time horizon exits a departure that has neither
reached the target nor stopped out, so undecided trades are not silently
counted as either range wins or move holds.
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
        reward_risk: float = 1.5,
        size: int = 1,
        max_hold_minutes: int = 60,
        entry_window_minutes: int = 120,
        flatten_margin_minutes: int = 5,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.reward_risk = reward_risk

        self.range_ns = range_minutes * 60 * NS_PER_SECOND
        self.entry_window_ns = entry_window_minutes * 60 * NS_PER_SECOND
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.flatten_margin_ns = flatten_margin_minutes * 60 * NS_PER_SECOND
        self.rth_open_offset_ns = rth_open_offset_seconds * NS_PER_SECOND
        self.rth_close_offset_ns = rth_close_offset_seconds * NS_PER_SECOND

        self._session: int | None = None
        self._range_high: float | None = None
        self._range_low: float | None = None
        self._range_done: bool = False
        self._daily_vol: float | None = None
        self._deadline_ns: int | None = None
        self._traded_this_session: bool = False
        self._close_queued: bool = False
        self._rth_open_ns: int = 0
        self._rth_close_ns: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def _update_daily_vol(self, ctx) -> None:
        hist = ctx.history(self.daily_interval)
        if len(hist) < self.trailing_days:
            self._daily_vol = None
            return
        days = hist.last(self.trailing_days)
        ranges = [b.high - b.low for b in days]
        self._daily_vol = sum(ranges) / len(ranges)

    def _update_range(self, bar) -> None:
        if self._range_high is None or bar.high > self._range_high:
            self._range_high = bar.high
        if self._range_low is None or bar.low < self._range_low:
            self._range_low = bar.low

    def _flatten_once(self, ctx) -> None:
        """Queue a flatten exactly once; a pending close is never re-cancelled."""
        if self._close_queued:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position:
            ctx.close()
            self._close_queued = True

    def _manage_open(self, ctx) -> None:
        if self._close_queued:
            return
        if not ctx.actionable:
            return

        if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
            self._flatten_once(ctx)
            return

        # Always flat before the regular-hours close so a position does not
        # ride into the Globex overnight.
        if ctx.ts >= self._rth_close_ns - self.flatten_margin_ns:
            self._flatten_once(ctx)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._rth_open_ns = ctx.session_start + self.rth_open_offset_ns
            self._rth_close_ns = ctx.session_start + self.rth_close_offset_ns
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._daily_vol = None
            self._deadline_ns = None
            self._traded_this_session = False
            self._close_queued = False

        if self._daily_vol is None:
            self._update_daily_vol(ctx)

        rth_open = self._rth_open_ns
        range_end = rth_open + self.range_ns

        if bar.close_ts <= rth_open:
            return

        # Accumulate the opening range. The closing bar at range_end completes
        # the range but is not a decision bar.
        if bar.close_ts < range_end:
            self._update_range(bar)
            return
        if bar.close_ts == range_end:
            self._update_range(bar)
            self._range_done = True
            return

        if not self._range_done:
            self._range_done = True
            # Fall through: a departure on the first post-range bar should be
            # eligible rather than silently skipped.

        if not ctx.actionable:
            return

        if ctx.position:
            self._manage_open(ctx)
            return

        if ctx.working_order_ids:
            return

        if self._traded_this_session:
            return

        if self._range_high is None or self._range_low is None:
            return
        if bar.close_ts > rth_open + self.entry_window_ns:
            return
        if self._daily_vol is None or self._daily_vol <= 0:
            return

        buffer = self.breakout_vol_mult * self._daily_vol
        if buffer <= 0:
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        if range_width <= 0:
            return

        stop_off = max(self.stop_mult * buffer, self.min_stop_frac * range_width, 2.0)

        side = 0
        target_off = 0.0
        if bar.close < self._range_low - buffer:
            side = 1  # fade a downside breakout
            distance_to_range = self._range_low - bar.close
            target_off = max(
                distance_to_range + 0.5 * range_width,
                stop_off * self.reward_risk,
            )
        elif bar.close > self._range_high + buffer:
            side = -1  # fade an upside breakout
            distance_to_range = bar.close - self._range_high
            target_off = max(
                distance_to_range + 0.5 * range_width,
                stop_off * self.reward_risk,
            )

        if side != 0:
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
                self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._deadline_ns = None
            self._close_queued = False
            return

        self._close_queued = False
        if self._deadline_ns is None:
            hard_deadline = self._rth_close_ns - self.flatten_margin_ns
            deadline = ctx.ts + self.max_hold_ns
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
