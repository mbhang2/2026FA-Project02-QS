"""Opening range fade with average opening range volatility and delayed entry.

Investigates whether price leaving the opening range tends to revert (range wins)
when the departure occurs after the initial post-open order flow has settled.
The opening range is the first 30 minutes of the regular trading hours (RTH)
session, defined as the high and low of the first 30 one-minute bars after the
RTH open (15.5 hours after the Globex session start). Volatility is the average
opening range (high-low) over the last 20 sessions, which is more stable than
intraday return standard deviation. A breakout is defined as a 1-minute bar
closing beyond the range by 0.5 times this average range. To avoid the losing
early hours identified in prior work, we only consider breakouts that occur
between 60 and 180 minutes after the RTH open. We fade the breakout: sell on an
upside breakout, buy on a downside breakout. Only the first breakout of each
session is traded. The position is protected by a bracket with stop and target
offsets of 1.0 and 1.5 times the average range. If neither stop nor target is
hit within 90 minutes, the position is closed at market. Positions are also
flattened 10 minutes before session end. This design aims to trade most sessions
while avoiding the noisy first hour after the open.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_range_minutes: int = 30,
        vol_lookback_sessions: int = 20,
        breakout_vol_mult: float = 0.5,
        stop_vol_mult: float = 1.0,
        target_vol_mult: float = 1.5,
        max_hold_minutes: int = 90,
        min_wait_minutes: int = 60,
        trade_window_minutes: int = 180,   # after RTH open
        session_close_margin_minutes: int = 10,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_minutes = opening_range_minutes
        self.vol_lookback_sessions = vol_lookback_sessions
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.min_wait_ns = min_wait_minutes * 60 * NS_PER_SECOND
        self.trade_window_ns = trade_window_minutes * 60 * NS_PER_SECOND
        self.session_close_margin_ns = session_close_margin_minutes * 60 * NS_PER_SECOND
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.entry_taken: bool = False
        self.entry_time: Optional[int] = None

        # Historical opening ranges for volatility
        self.opening_ranges: deque[float] = deque(maxlen=self.vol_lookback_sessions)
        self.avg_opening_range: Optional[float] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state and update average opening range."""
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
        if len(self.opening_ranges) >= self.vol_lookback_sessions:
            self.avg_opening_range = sum(self.opening_ranges) / len(self.opening_ranges)
        else:
            self.avg_opening_range = None

        # Reset session state
        self.current_session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * 3600 * NS_PER_SECOND)
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
        opening_range_end = self.rth_open_ts + self.opening_range_minutes * 60 * NS_PER_SECOND
        if bar.open_ts >= opening_range_end:
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
        if ctx.position:
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

        # Update opening range
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
        if self.avg_opening_range is None or self.avg_opening_range <= 0:
            return

        # Time filter: only trade between min_wait and trade_window after RTH open
        time_since_open = ctx.ts - self.rth_open_ts
        if time_since_open < self.min_wait_ns or time_since_open > self.trade_window_ns:
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
