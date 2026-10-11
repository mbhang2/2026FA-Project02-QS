"""Opening range fade strategy.

Investigates whether price leaving the opening range tends to revert (range wins)
rather than continue (move holds). The opening range is the first 30 minutes of
the regular trading hours (RTH) session, defined as the high and low of the first
six 5-minute bars after the RTH open (15.5 hours after the Globex session start).
A breakout is defined as price exceeding the range by a volatility-scaled
threshold: 0.5 times the trailing standard deviation of 5-minute returns.
When a breakout occurs within the first 3 hours of RTH, we fade it: sell on an
upside breakout, buy on a downside breakout. The position is protected by a
bracket with stop and target offsets also scaled by volatility (1.0 and 1.5
times the standard deviation, respectively). If neither stop nor target is hit
within 2 hours, the position is closed at market (undetermined outcome).
Positions are also flattened 5 minutes before session end. The strategy trades
every session where the opening range is established and a breakout occurs,
using only intraday data so it is exposed from the first session.
"""

from __future__ import annotations

import statistics
from collections import deque
from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_range_bars: int = 6,          # 30 minutes at 5m
        breakout_vol_mult: float = 0.5,
        stop_vol_mult: float = 1.0,
        target_vol_mult: float = 1.5,
        min_vol_samples: int = 20,            # 100 minutes of 5m returns
        max_hold_minutes: int = 120,
        trade_window_minutes: int = 180,      # only trade first 3 hours of RTH
        session_close_margin_seconds: int = 300,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_bars
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.min_vol_samples = min_vol_samples
        self.max_hold_ns = max_hold_minutes * 60 * NS_PER_SECOND
        self.trade_window_ns = trade_window_minutes * 60 * NS_PER_SECOND
        self.session_close_margin_ns = session_close_margin_seconds * NS_PER_SECOND
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.bars_since_rth_open: int = 0
        self.opening_range_high: float = float("-inf")
        self.opening_range_low: float = float("inf")
        self.opening_range_ready: bool = False

        # Volatility state
        self.last_close: Optional[float] = None
        self.returns: deque[float] = deque(maxlen=2000)

        # Trade state
        self.entry_pending: bool = False
        self.entry_order_id: Optional[int] = None
        self.trade_active: bool = False
        self.entry_time: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state for a new session."""
        self.current_session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + 55_800 * NS_PER_SECOND
        self.bars_since_rth_open = 0
        self.opening_range_high = float("-inf")
        self.opening_range_low = float("inf")
        self.opening_range_ready = False
        self.entry_pending = False
        self.entry_order_id = None
        self.trade_active = False
        self.entry_time = 0

    def _update_volatility(self, bar) -> None:
        """Update trailing returns from 5-minute bar closes."""
        if self.last_close is not None and self.last_close != 0:
            ret = (bar.close - self.last_close) / self.last_close
            self.returns.append(ret)
        self.last_close = bar.close

    def _exit_position(self, ctx) -> None:
        """Cancel bracket legs and flatten the position."""
        if self.entry_order_id is not None:
            legs = ctx.find_orders(parent_id=self.entry_order_id)
            for leg in legs:
                ctx.cancel(leg.id)
        ctx.close()
        self.trade_active = False
        self.entry_order_id = None
        self.entry_time = 0

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Update volatility
        self._update_volatility(bar)

        # Track RTH open and bar count
        if bar.open_ts >= self.rth_open_ts:
            if self.bars_since_rth_open == 0:
                # First bar of RTH
                self.bars_since_rth_open = 1
            else:
                self.bars_since_rth_open += 1

            # Build opening range
            if not self.opening_range_ready and self.bars_since_rth_open <= self.opening_range_bars:
                self.opening_range_high = max(self.opening_range_high, bar.high)
                self.opening_range_low = min(self.opening_range_low, bar.low)
                if self.bars_since_rth_open == self.opening_range_bars:
                    self.opening_range_ready = True

        # Trading logic only when actionable
        if not ctx.actionable:
            return

        # Manage existing position
        if ctx.position or ctx.working_order_ids:
            if self.trade_active and ctx.position != 0:
                # Time-based exit: max hold
                if ctx.ts - self.entry_time > self.max_hold_ns:
                    self._exit_position(ctx)
                    return
                # Session-end exit
                if ctx.session_end - ctx.ts < self.session_close_margin_ns:
                    self._exit_position(ctx)
                    return
            return

        # No position, no working orders: consider new entry
        if not self.opening_range_ready:
            return
        if self.bars_since_rth_open > self.opening_range_bars + self.trade_window_ns // (5 * 60 * NS_PER_SECOND):
            # Outside trade window (first 3 hours after RTH open)
            return
        if len(self.returns) < self.min_vol_samples:
            return

        vol = statistics.stdev(self.returns)
        if vol <= 0:
            return
        threshold = self.breakout_vol_mult * vol * bar.close

        # Fade logic: sell upside breakout, buy downside breakout
        if bar.close > self.opening_range_high + threshold:
            self.entry_order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_vol_mult * vol * bar.close,
                target_offset=self.target_vol_mult * vol * bar.close,
                tag="fade_short",
            )
            if self.entry_order_id is not None:
                self.entry_pending = True
        elif bar.close < self.opening_range_low - threshold:
            self.entry_order_id = ctx.buy(
                size=self.size,
                stop_offset=self.stop_vol_mult * vol * bar.close,
                target_offset=self.target_vol_mult * vol * bar.close,
                tag="fade_long",
            )
            if self.entry_order_id is not None:
                self.entry_pending = True

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if self.entry_pending and self.entry_order_id is not None and fill.order_id == self.entry_order_id:
            # Entry filled
            self.entry_pending = False
            self.trade_active = True
            self.entry_time = ctx.ts
        elif self.trade_active and ctx.position == 0:
            # Position closed (stop, target, or manual exit)
            self.trade_active = False
            self.entry_order_id = None
            self.entry_time = 0

    def on_lifecycle(self, ctx) -> None:
        # Clear pending entry if the order is rejected or expires
        if self.entry_pending and self.entry_order_id is not None:
            if ctx.event.order_id == self.entry_order_id and ctx.event.kind in (
                "order_rejected",
                "day_expired",
                "cancel_confirmed",
            ):
                self.entry_pending = False
                self.entry_order_id = None
