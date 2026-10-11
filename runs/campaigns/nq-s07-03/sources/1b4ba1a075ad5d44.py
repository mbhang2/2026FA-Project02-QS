"""Opening range fade with average-range volatility and moderate filters.

Investigates whether price leaving the opening range tends to revert (range wins).
The opening range is the first 30 minutes of the regular trading hours (RTH)
session, defined as the high and low of the first 30 one-minute bars after the
RTH open (15.5 hours after the Globex session start). Volatility is the average
opening range (high-low) over the last 20 sessions, which is more stable than
trailing 1-minute returns. A fade entry is taken when a 1-minute bar closes
beyond the range by 0.5 times the average range, but only if the breakout
distance is not too large (<= 3.0 times average range) to avoid strong trends.
No time filter is applied beyond the natural session boundaries, preserving
exposure. The position is protected by a bracket with stop and target offsets
of 1.0 and 1.5 times the average range. If neither stop nor target is hit
within 120 minutes, the position is closed at market. Positions are also
flattened 5 minutes before session end. Only one trade per session is allowed.
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
        opening_range_minutes: int = 30,
        vol_lookback_sessions: int = 20,
        breakout_vol_mult: float = 0.5,
        max_breakout_vol_mult: float = 3.0,
        stop_vol_mult: float = 1.0,
        target_vol_mult: float = 1.5,
        max_hold_minutes: int = 120,
        session_close_margin_seconds: int = 300,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_minutes  # 1m bars
        self.vol_lookback_sessions = vol_lookback_sessions
        self.breakout_vol_mult = breakout_vol_mult
        self.max_breakout_vol_mult = max_breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.session_close_margin_ns = session_close_margin_seconds * NS_PER_SECOND
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.entry_taken: bool = False

        # Volatility state (average opening range)
        self.opening_ranges: deque[float] = deque(maxlen=self.vol_lookback_sessions)
        self.avg_opening_range: Optional[float] = None

        # Trade state
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None

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

        # Update average opening range if enough history
        if len(self.opening_ranges) >= self.vol_lookback_sessions:
            self.avg_opening_range = sum(self.opening_ranges) / len(self.opening_ranges)
        else:
            self.avg_opening_range = None

        # Reset session state
        self.current_session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * NS_PER_HOUR)
        self.range_high = None
        self.range_low = None
        self.range_ready = False
        self.entry_taken = False
        self.entry_time = None
        self.entry_order_id = None

    def _update_opening_range(self, bar) -> None:
        """Accumulate high/low from bars within the opening range window."""
        if self.range_ready or self.rth_open_ts == 0:
            return
        if bar.open_ts < self.rth_open_ts:
            return
        if bar.open_ts >= self.rth_open_ts + self.opening_range_bars * NS_PER_MINUTE:
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
            # Find working bracket legs and cancel them
            legs = ctx.find_orders(parent_id=self.entry_order_id)
            for leg in legs:
                ctx.cancel(leg.id)
        if ctx.position:
            ctx.close()
        self.entry_order_id = None
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
        if self.avg_opening_range is None:
            return

        avg_range = self.avg_opening_range
        threshold = self.breakout_vol_mult * avg_range
        stop_offset = self.stop_vol_mult * avg_range
        target_offset = self.target_vol_mult * avg_range

        # Fade the breakout: sell on upside breakout, buy on downside breakout
        # Only if breakout distance is not too large (avoid strong trends)
        if bar.close > self.range_high + threshold:
            breakout_dist = bar.close - self.range_high
            if breakout_dist <= self.max_breakout_vol_mult * avg_range:
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
            breakout_dist = self.range_low - bar.close
            if breakout_dist <= self.max_breakout_vol_mult * avg_range:
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
