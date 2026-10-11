"""Opening range fade with limit orders at volatility-scaled levels.

Investigates whether price leaving the opening range tends to revert (range wins).
The opening range is the first 30 minutes of the regular trading hours (RTH)
session, defined as the high and low of the first six 5-minute bars after the
RTH open (15.5 hours after the Globex session start). Volatility is the average
true range of the opening range over the last 20 sessions. A fade entry is
placed as a limit order at the range boundary plus/minus 0.3 times this average
range: sell limit above the range, buy limit below. The position is protected by
a bracket with stop and target offsets of 1.0 and 1.5 times the average range.
If neither order fills within 3 hours of the RTH open, both are cancelled. If
one fills, the other is cancelled immediately. Positions are also flattened 5
minutes before session end. The strategy trades every session after the first
20, which provides the volatility estimate.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_range_bars: int = 6,          # 30 minutes at 5m
        vol_lookback: int = 20,               # sessions for average opening range
        breakout_vol_mult: float = 0.3,       # threshold as fraction of avg range
        stop_vol_mult: float = 1.0,
        target_vol_mult: float = 1.5,
        max_hold_minutes: int = 60,
        trade_window_minutes: int = 180,      # cancel unfilled orders after this
        session_close_margin_seconds: int = 300,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_bars
        self.vol_lookback = vol_lookback
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
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
        self.avg_opening_range: Optional[float] = None

        # Historical opening ranges for volatility
        self.opening_ranges: deque[float] = deque(maxlen=self.vol_lookback)

        # Trade state
        self.entry_orders_placed: bool = False
        self.short_order_id: Optional[int] = None
        self.long_order_id: Optional[int] = None
        self.trade_active: bool = False
        self.entry_time: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state and update volatility estimate."""
        # Record previous session's opening range if valid
        if (
            self.current_session is not None
            and self.opening_range_ready
            and self.opening_range_high != float("-inf")
            and self.opening_range_low != float("inf")
        ):
            rng = self.opening_range_high - self.opening_range_low
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
        self.rth_open_ts = ctx.session_start + 55_800 * NS_PER_SECOND
        self.bars_since_rth_open = 0
        self.opening_range_high = float("-inf")
        self.opening_range_low = float("inf")
        self.opening_range_ready = False
        self.entry_orders_placed = False
        self.short_order_id = None
        self.long_order_id = None
        self.trade_active = False
        self.entry_time = 0

    def _cancel_entry_orders(self, ctx) -> None:
        """Cancel any unfilled entry limit orders."""
        if self.short_order_id is not None:
            ctx.cancel(self.short_order_id)
            self.short_order_id = None
        if self.long_order_id is not None:
            ctx.cancel(self.long_order_id)
            self.long_order_id = None
        self.entry_orders_placed = False

    def _exit_position(self, ctx) -> None:
        """Cancel bracket legs and flatten the position."""
        # Cancel any working orders (bracket legs) by finding them via parent
        # We don't store parent id, but we can cancel all working orders
        ctx.cancel_all()
        ctx.close()
        self.trade_active = False
        self.entry_time = 0

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Track RTH open and bar count
        if bar.open_ts >= self.rth_open_ts:
            if self.bars_since_rth_open == 0:
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

        # No position, no working orders: consider placing entry orders
        if not self.opening_range_ready or self.avg_opening_range is None:
            return

        # Check if we are still within the trade window
        if self.bars_since_rth_open > self.opening_range_bars + int(
            self.trade_window_ns / (5 * 60 * NS_PER_SECOND)
        ):
            # Past trade window, cancel any unfilled orders (should already be cancelled)
            if self.entry_orders_placed:
                self._cancel_entry_orders(ctx)
            return

        # Place entry orders once after range is ready
        if not self.entry_orders_placed:
            threshold = self.breakout_vol_mult * self.avg_opening_range
            stop_offset = self.stop_vol_mult * self.avg_opening_range
            target_offset = self.target_vol_mult * self.avg_opening_range

            # Sell limit above range (fade upside breakout)
            self.short_order_id = ctx.sell(
                size=self.size,
                type="limit",
                limit=self.opening_range_high + threshold,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade_short",
            )
            # Buy limit below range (fade downside breakout)
            self.long_order_id = ctx.buy(
                size=self.size,
                type="limit",
                limit=self.opening_range_low - threshold,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade_long",
            )
            self.entry_orders_placed = True

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        # Check if this fill is one of our entry orders
        if self.entry_orders_placed:
            if fill.order_id == self.short_order_id:
                # Short entry filled, cancel the long entry
                if self.long_order_id is not None:
                    ctx.cancel(self.long_order_id)
                    self.long_order_id = None
                self.short_order_id = None
                self.entry_orders_placed = False
                self.trade_active = True
                self.entry_time = ctx.ts
            elif fill.order_id == self.long_order_id:
                # Long entry filled, cancel the short entry
                if self.short_order_id is not None:
                    ctx.cancel(self.short_order_id)
                    self.short_order_id = None
                self.long_order_id = None
                self.entry_orders_placed = False
                self.trade_active = True
                self.entry_time = ctx.ts
        # If position closed (bracket leg or manual exit)
        if self.trade_active and ctx.position == 0:
            self.trade_active = False
            self.entry_time = 0

    def on_lifecycle(self, ctx) -> None:
        event = ctx.event
        # Clear order ids if they are cancelled or expire
        if event.order_id == self.short_order_id and event.kind in (
            "cancel_confirmed",
            "day_expired",
            "order_rejected",
        ):
            self.short_order_id = None
        if event.order_id == self.long_order_id and event.kind in (
            "cancel_confirmed",
            "day_expired",
            "order_rejected",
        ):
            self.long_order_id = None
        # If both entry orders are gone, mark as not placed
        if self.short_order_id is None and self.long_order_id is None:
            self.entry_orders_placed = False
