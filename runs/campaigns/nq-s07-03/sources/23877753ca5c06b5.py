"""Opening range fade strategy.

Investigates whether price leaving the opening range tends to revert (range wins)
rather than continue (move holds). The opening range is the first 30 minutes of
the regular trading hours (RTH) session, defined as the high and low of the first
six 5-minute bars after the RTH open (15.5 hours after the Globex session start).
A breakout is defined as price exceeding the range by a volatility-scaled
threshold: 0.5 times the average true range (ATR) of the previous 20 sessions.
When a breakout occurs within the first 3 hours of RTH, we fade it: sell on an
upside breakout, buy on a downside breakout. The position is protected by a
bracket with stop and target offsets also scaled by ATR (1.0 and 1.5 times ATR,
respectively). If neither stop nor target is hit within 2 hours, the position is
closed at market (undetermined outcome). The strategy trades only when at least
20 prior sessions of daily ranges are available for ATR calculation.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

NS_PER_SECOND = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_SECOND
NS_PER_HOUR = 3600 * NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_range_minutes: int = 30,
        atr_lookback: int = 20,
        breakout_mult: float = 0.5,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        max_hold_minutes: int = 120,
        trade_window_minutes: int = 180,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_minutes // 5  # assumes 5m bars
        self.atr_lookback = atr_lookback
        self.breakout_mult = breakout_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.trade_window_bars = trade_window_minutes // 5
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.session_ranges: deque[float] = deque(maxlen=self.atr_lookback)
        self.current_session_high: float = float("-inf")
        self.current_session_low: float = float("inf")
        self.rth_open_ts: int = 0
        self.rth_started: bool = False
        self.bars_since_rth_open: int = 0
        self.opening_range_high: float = float("-inf")
        self.opening_range_low: float = float("inf")
        self.opening_range_ready: bool = False
        self.atr: Optional[float] = None

        # Trade state
        self.entry_order_id: Optional[int] = None
        self.entry_pending: bool = False
        self.trade_active: bool = False
        self.entry_time: int = 0
        self.entry_price: float = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _end_session(self) -> None:
        """Finalize the previous session: record its range and update ATR."""
        if self.current_session is not None:
            if self.current_session_high != float("-inf") and self.current_session_low != float("inf"):
                rng = self.current_session_high - self.current_session_low
                self.session_ranges.append(rng)
            if len(self.session_ranges) >= self.atr_lookback:
                self.atr = sum(self.session_ranges) / len(self.session_ranges)
            else:
                self.atr = None

    def _start_session(self, ctx) -> None:
        """Reset per-session state for a new session."""
        self.current_session = ctx.session
        self.current_session_high = float("-inf")
        self.current_session_low = float("inf")
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * NS_PER_HOUR)
        self.rth_started = False
        self.bars_since_rth_open = 0
        self.opening_range_high = float("-inf")
        self.opening_range_low = float("inf")
        self.opening_range_ready = False
        # Reset trade state
        self.entry_order_id = None
        self.entry_pending = False
        self.trade_active = False

    def on_bars(self, ctx) -> None:
        # Detect session change
        if ctx.session != self.current_session:
            self._end_session()
            self._start_session(ctx)

        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Update session high/low
        self.current_session_high = max(self.current_session_high, bar.high)
        self.current_session_low = min(self.current_session_low, bar.low)

        # Track RTH start and bar count
        if not self.rth_started and bar.close_ts >= self.rth_open_ts:
            self.rth_started = True
            self.bars_since_rth_open = 1
        elif self.rth_started:
            self.bars_since_rth_open += 1

        # Build opening range
        if self.rth_started and self.bars_since_rth_open <= self.opening_range_bars:
            self.opening_range_high = max(self.opening_range_high, bar.high)
            self.opening_range_low = min(self.opening_range_low, bar.low)
            if self.bars_since_rth_open == self.opening_range_bars:
                self.opening_range_ready = True

        # Trading logic only when actionable
        if not ctx.actionable:
            return

        # If we have a position or working orders, manage existing trade
        if ctx.position or ctx.working_order_ids:
            if self.trade_active and ctx.position != 0:
                # Check timeout
                if ctx.ts - self.entry_time > self.max_hold_ns:
                    ctx.close()
                    self.trade_active = False
            return

        # No position, no working orders: consider new entry
        if not self.opening_range_ready or self.atr is None:
            return
        if self.bars_since_rth_open > self.trade_window_bars:
            return

        threshold = self.breakout_mult * self.atr
        if bar.high > self.opening_range_high + threshold:
            # Upside breakout -> fade short
            self.entry_order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_mult * self.atr,
                target_offset=self.target_mult * self.atr,
                tag="fade",
            )
            if self.entry_order_id is not None:
                self.entry_pending = True
        elif bar.low < self.opening_range_low - threshold:
            # Downside breakout -> fade long
            self.entry_order_id = ctx.buy(
                size=self.size,
                stop_offset=self.stop_mult * self.atr,
                target_offset=self.target_mult * self.atr,
                tag="fade",
            )
            if self.entry_order_id is not None:
                self.entry_pending = True

    def on_fill(self, ctx) -> None:
        if self.entry_pending and self.entry_order_id is not None and ctx.fill.order_id == self.entry_order_id:
            # Entry filled
            self.entry_pending = False
            self.trade_active = True
            self.entry_time = ctx.ts
            self.entry_price = ctx.fill.price
        elif self.trade_active and ctx.position == 0:
            # Bracket leg or manual close filled, position flat
            self.trade_active = False
            self.entry_order_id = None

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
