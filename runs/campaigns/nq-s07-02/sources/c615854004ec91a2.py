"""Opening-range breakout, one trade per session, with explicit time horizon.

This is a refinement of the completed build (#0) that scored -0.0068. The
change is modest and deliberate: the previous build could re-enter after its
bracket leg filled, producing 483 trades over 384 sessions. That overtrading
paid commission without a corresponding edge.

This strategy enforces one filled entry per session. It otherwise keeps the
same logic: the opening range is the high/low of the first 30 minutes of the
regular-hours session; the departure buffer is scaled by trailing daily
volatility; the entry window is shortened from 120 to 90 minutes; the
breakout buffer is widened slightly to 0.05 of trailing daily volatility.

The time horizon is explicit: a position still open after 75 minutes, or five
minutes before the regular-hours close, is flattened. Undecided departures are
not silently counted as range wins or move holds.
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
        breakout_vol_mult: float = 0.05,
        stop_mult: float = 2.5,
        min_stop_frac: float = 0.5,
        reward_risk: float = 1.5,
        target_mult: float = 3.0,
        size: int = 1,
        max_hold_minutes: int = 75,
        entry_window_minutes: int = 90,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.size = size
        self.trailing_days = trailing_days
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.reward_risk = reward_risk
        self.target_mult = target_mult

        self.range_ns = range_minutes * 60 * NS_PER_SECOND
        self.entry_window_ns = entry_window_minutes * 60 * NS_PER_SECOND
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.rth_open_offset_ns = rth_open_offset_seconds * NS_PER_SECOND
        self.rth_close_offset_ns = rth_close_offset_seconds * NS_PER_SECOND

        self._session = None
        self._range_high = None
        self._range_low = None
        self._range_done = False
        self._daily_vol = None
        self._deadline_ns = None
        self._traded = False

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

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._range_done = False
            self._daily_vol = None
            self._deadline_ns = None
            self._traded = False

        if self._daily_vol is None:
            self._update_daily_vol(ctx)

        rth_open = ctx.session_start + self.rth_open_offset_ns
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

        if not ctx.actionable:
            return

        if ctx.position:
            if self._deadline_ns is not None and ctx.ts >= self._deadline_ns:
                if ctx.working_order_ids:
                    ctx.cancel_all()
                ctx.close()
                self._deadline_ns = None
            return

        if ctx.working_order_ids:
            return

        # Enforce one filled entry per session. Once we have had a fill, we do
        # not put another entry on even after the bracket has flattened us.
        if self._traded:
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
            side = 1
        elif bar.close < self._range_low - buffer:
            side = -1
        if side == 0:
            return

        range_width = max(self._range_high - self._range_low, 0.0)
        stop_off = max(self.stop_mult * buffer, self.min_stop_frac * range_width, 2.0)
        target_off = max(self.target_mult * buffer, stop_off * self.reward_risk)
        target_off = max(target_off, stop_off * 1.1)

        if side > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="orb-short",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._deadline_ns = None
            return

        # This was an entry fill, so the session's one trade is now consumed.
        self._traded = True

        if self._deadline_ns is None:
            rth_close = ctx.session_start + self.rth_close_offset_ns
            deadline = ctx.ts + self.max_hold_ns
            hard_deadline = rth_close - 5 * 60 * NS_PER_SECOND
            if hard_deadline > ctx.ts and hard_deadline < deadline:
                deadline = hard_deadline
            self._deadline_ns = deadline

    def on_lifecycle(self, ctx) -> None:
        pass
