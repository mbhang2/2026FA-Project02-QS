"""Opening range fade with average true range volatility and time filter.

Investigates whether price leaving the opening range tends to revert (range wins).
The opening range is the first 30 minutes of the regular trading hours (RTH)
session, defined as the high and low of the first six 5-minute bars after the
RTH open (15.5 hours after the Globex session start). Volatility is the average
true range of the opening range over the last 20 sessions, which is more stable
than trailing returns and directly tied to the range being traded. A fade entry
is taken when a 5-minute bar closes beyond the range by 0.5 times this average
range. To avoid the losing early hours identified in prior work, we only
consider breakouts that occur between 60 and 180 minutes after the RTH open.
The position is protected by a bracket with stop and target offsets of 1.0 and
2.0 times the average range. If neither stop nor target is hit within 60
minutes, the position is closed at market. Positions are also flattened 5
minutes before session end. Only the first breakout of each session is traded.
This design aims to trade most sessions while reducing noise and avoiding the
worst time periods.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from src.core.types import NS_PER_SECOND

NS_PER_MINUTE = 60 * NS_PER_SECOND
NS_PER_HOUR = 3600 * NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_range_bars: int = 6,          # 30 minutes at 5m
        vol_lookback: int = 20,               # sessions for average opening range
        breakout_vol_mult: float = 0.5,
        stop_vol_mult: float = 1.0,
        target_vol_mult: float = 2.0,
        min_wait_bars: int = 12,              # 60 minutes after RTH open
        max_wait_bars: int = 36,              # 180 minutes after RTH open
        max_hold_bars: int = 12,              # 60 minutes max hold
        session_close_margin_bars: int = 1,   # 5 minutes before session end
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_bars
        self.vol_lookback = vol_lookback
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.min_wait_ns = min_wait_bars * 5 * NS_PER_MINUTE
        self.max_wait_ns = max_wait_bars * 5 * NS_PER_MINUTE
        self.max_hold_ns = max_hold_bars * 5 * NS_PER_MINUTE
        self.session_close_margin_ns = session_close_margin_bars * 5 * NS_PER_MINUTE
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.bars_since_rth_open: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.entry_taken: bool = False
        self.entry_time: Optional[int] = None

        # Volatility state: average opening range over prior sessions
        self.opening_ranges: deque[float] = deque(maxlen=self.vol_lookback)
        self.avg_opening_range: Optional[float] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state and update volatility estimate."""
        # Record previous session's opening range if valid
        if (
            self.current_session is not None
            and self.range_ready
            and self.range_high is not None
            and self.range_low is not None
        ):
            rng = self.range_high - self.range_low
            if rng > 0:
                self.opening_ranges.append(rng)

        # Compute average opening range if enough history
        if len(self.opening_ranges) >= self.vol_lookback:
            self.avg_opening_range = sum(self.opening_ranges) / len(self.opening_ranges)
        else:
            self.avg_opening_range = None

        # Reset session state
        self.current_session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * NS_PER_HOUR)
        self.bars_since_rth_open = 0
        self.range_high = None
        self.range_low = None
        self.range_ready = False
        self.entry_taken = False
        self.entry_time = None

    def _update_opening_range(self, bar) -> None:
        """Accumulate high/low from bars within the opening range window."""
        if self.range_ready:
            return
        if bar.open_ts < self.rth_open_ts:
            return
        if bar.open_ts >= self.rth_open_ts + self.opening_range_bars * 5 * NS_PER_MINUTE:
            self.range_ready = True
            return
        if self.range_high is None:
            self.range_high = bar.high
            self.range_low = bar.low
        else:
            self.range_high = max(self.range_high, bar.high)
            self.range_low = min(self.range_low, bar.low)

    def _exit_position(self, ctx) -> None:
        """Cancel all working orders and flatten the position."""
        ctx.cancel_all()
        ctx.close()
        self.entry_time = None

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        # Only process our interval
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Track bars since RTH open
        if bar.open_ts >= self.rth_open_ts:
            self.bars_since_rth_open += 1

        # Build opening range
        self._update_opening_range(bar)

        # Trading logic only when actionable
        if not ctx.actionable:
            return

        # Manage existing position
        if ctx.position:
            # Time-based exit
            if self.entry_time is not None and ctx.ts - self.entry_time > self.max_hold_ns:
                self._exit_position(ctx)
                return
            # Session-end exit
            if ctx.session_end - ctx.ts < self.session_close_margin_ns:
                self._exit_position(ctx)
                return
            return  # let bracket manage

        # No position, no working orders: consider new entry
        if ctx.working_order_ids:
            return
        if self.entry_taken:
            return
        if not self.range_ready or self.range_high is None or self.range_low is None:
            return
        if self.avg_opening_range is None:
            return

        # Time filter: only trade within the specified window after RTH open
        time_since_open = ctx.ts - self.rth_open_ts
        if time_since_open < self.min_wait_ns or time_since_open > self.max_wait_ns:
            return

        threshold = self.breakout_vol_mult * self.avg_opening_range
        stop_offset = self.stop_vol_mult * self.avg_opening_range
        target_offset = self.target_vol_mult * self.avg_opening_range

        # Fade the breakout: sell on upside breakout, buy on downside breakout
        if bar.close > self.range_high + threshold:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade_short",
            )
            if order_id is not None:
                self.entry_taken = True
        elif bar.close < self.range_low - threshold:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade_long",
            )
            if order_id is not None:
                self.entry_taken = True

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position != 0 and self.entry_time is None:
            # Entry fill
            self.entry_time = ctx.ts
        elif fill.position == 0:
            # Position closed (stop, target, or manual exit)
            self.entry_time = None

    def on_lifecycle(self, ctx) -> None:
        pass
