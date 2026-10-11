"""Opening range fade strategy using daily true range volatility.

Investigates whether price leaving the opening range tends to revert (range wins)
rather than continue (move holds). The opening range is the first 30 minutes of
the regular trading hours (RTH) session, defined as the high and low of the first
30 one-minute bars after the RTH open (15.5 hours after the Globex session start).
Volatility is measured by the average true range of daily bars over the last 20
sessions, which provides a stable, cross-session estimate of realized volatility
as recommended by the campaign. A breakout is defined as a 1-minute bar closing
beyond the range by 0.25 times this daily volatility. We fade the breakout: sell
on an upside breakout, buy on a downside breakout. Only the first breakout of each
session is traded, and only after a minimum wait of 30 minutes after the RTH open
to avoid the noisiest early period. The trade window extends 120 minutes after the
opening range is established. The position is protected by a bracket with stop and
target offsets of 0.5 and 1.0 times the daily volatility, respectively. If neither
stop nor target is hit within 120 minutes, the position is closed at market.
Positions are also flattened 5 minutes before session end. This design aims to
trade most sessions while using a more robust volatility measure and avoiding
early false breakouts.
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
        interval: str = "1m",
        daily_interval: str = "1d",
        opening_range_minutes: int = 30,
        daily_vol_lookback: int = 20,
        breakout_vol_mult: float = 0.25,
        stop_vol_mult: float = 0.5,
        target_vol_mult: float = 1.0,
        min_wait_minutes: int = 30,
        max_hold_minutes: int = 120,
        trade_window_minutes: int = 120,
        session_close_margin_minutes: int = 5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_range_bars = opening_range_minutes  # 1m bars
        self.daily_vol_lookback = daily_vol_lookback
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.min_wait_ns = min_wait_minutes * NS_PER_MINUTE
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.trade_window_ns = trade_window_minutes * NS_PER_MINUTE
        self.session_close_margin_ns = session_close_margin_minutes * NS_PER_MINUTE
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.entry_taken: bool = False

        # Daily volatility state (persists across sessions)
        self.daily_true_ranges: deque[float] = deque(maxlen=self.daily_vol_lookback)
        self.last_daily_close: Optional[float] = None
        self.daily_vol: Optional[float] = None

        # Trade state
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state and compute daily volatility from stored data."""
        self.current_session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * NS_PER_HOUR)
        self.range_high = None
        self.range_low = None
        self.range_ready = False
        self.entry_taken = False
        self.entry_time = None
        self.entry_order_id = None

        # Compute daily volatility if enough daily bars have been collected
        if len(self.daily_true_ranges) >= self.daily_vol_lookback:
            self.daily_vol = sum(self.daily_true_ranges) / len(self.daily_true_ranges)
        else:
            self.daily_vol = None

    def _update_daily_state(self, bar) -> None:
        """Update daily true range when a daily bar closes."""
        if self.last_daily_close is None:
            # First daily bar: use high-low as true range
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self.last_daily_close),
                abs(bar.low - self.last_daily_close),
            )
        self.daily_true_ranges.append(tr)
        self.last_daily_close = bar.close

    def _update_opening_range(self, bar) -> None:
        """Accumulate high/low from bars within the opening range window."""
        if self.range_ready:
            return
        if bar.open_ts < self.rth_open_ts:
            return
        opening_range_end = self.rth_open_ts + self.opening_range_bars * NS_PER_MINUTE
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
        """Cancel bracket legs and flatten the position."""
        if self.entry_order_id is not None:
            legs = ctx.find_orders(parent_id=self.entry_order_id)
            for leg in legs:
                ctx.cancel(leg.id)
        if ctx.position:
            ctx.close()
        self.entry_order_id = None
        self.entry_time = None

    def _cancel_entry_if_stale(self, ctx) -> None:
        """Cancel a working entry order if it is past the trade window or near session end."""
        if self.entry_order_id is None:
            return
        # Check if we are past the trade window
        trade_window_end = self.rth_open_ts + (self.opening_range_bars * NS_PER_MINUTE + self.trade_window_ns)
        if ctx.ts > trade_window_end:
            ctx.cancel(self.entry_order_id)
            self.entry_order_id = None
            return
        # Check if near session end
        if ctx.session_end - ctx.ts < self.session_close_margin_ns:
            ctx.cancel(self.entry_order_id)
            self.entry_order_id = None

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        # Process daily bar if it closed
        if self.daily_interval in ctx.bars:
            daily_bar = ctx.bars[self.daily_interval]
            self._update_daily_state(daily_bar)

        # Process 1-minute bar if it closed
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

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

        # No position: handle working entry order
        if ctx.working_order_ids:
            # If we have a working entry order, check if it should be cancelled
            self._cancel_entry_if_stale(ctx)
            return

        # No position, no working orders: consider new entry
        if self.entry_taken:
            return
        if not self.range_ready or self.range_high is None or self.range_low is None:
            return
        if self.daily_vol is None or self.daily_vol <= 0:
            return

        # Time filter: minimum wait after RTH open
        time_since_open = ctx.ts - self.rth_open_ts
        if time_since_open < self.min_wait_ns:
            return

        # Check if we are still within the trade window
        trade_window_end = self.rth_open_ts + (self.opening_range_bars * NS_PER_MINUTE + self.trade_window_ns)
        if bar.close_ts > trade_window_end:
            return

        threshold = self.breakout_vol_mult * self.daily_vol
        stop_offset = self.stop_vol_mult * self.daily_vol
        target_offset = self.target_vol_mult * self.daily_vol

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
                self.entry_order_id = order_id
        elif bar.close < self.range_low - threshold:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade_long",
            )
            if order_id is not None:
                self.entry_taken = True
                self.entry_order_id = order_id

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position != 0 and self.entry_time is None:
            # Entry fill
            self.entry_time = ctx.ts
        elif fill.position == 0:
            # Position closed (stop, target, or manual exit)
            self.entry_time = None
            self.entry_order_id = None

    def on_lifecycle(self, ctx) -> None:
        event = ctx.event
        if event.order_id == self.entry_order_id and event.kind in (
            "day_expired",
            "cancel_confirmed",
            "order_rejected",
        ):
            self.entry_order_id = None
            self.entry_time = None
