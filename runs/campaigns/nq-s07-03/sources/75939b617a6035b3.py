"""Opening range fade with ATR-based volatility, adaptive target, and distance filter.

Investigates whether price leaving the opening range tends to revert (range wins).
The opening range is the first 30 minutes of the regular trading hours (RTH)
session, defined as the high and low of the first 30 one-minute bars after the
RTH open (15.5 hours after the Globex session start). Volatility is measured by
the median true range (ATR) of the last 100 one-minute bars, a robust measure of
typical bar range that captures intra-bar movement better than close-to-close
returns. A breakout is defined as a 1-minute bar closing beyond the range by 0.8
times this ATR. To avoid chasing extended moves, we require the breakout bar's
close to be no more than 2.0 times ATR beyond the range boundary. The target is
adaptive: for a short, it is the distance from the entry to the range low (or a
minimum of 1.0 ATR), aiming to exit when price returns to the range; for a long,
it is the distance to the range high. The stop is fixed at 0.8 ATR. The bar-range
filter skips bars whose high-low range exceeds 3.0 ATR. Entries are taken at
least 15 minutes after the range is ready and within 120 minutes. Only the first
breakout of each session is traded. Positions are closed after 120 minutes or 5
minutes before session end. This design aims to improve on the prior best (#30)
by using a more robust volatility measure and an adaptive target that directly
targets the range boundary.
"""

from __future__ import annotations

import statistics
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
        vol_lookback: int = 100,
        breakout_vol_mult: float = 0.8,
        stop_vol_mult: float = 0.8,
        min_target_vol_mult: float = 1.0,
        max_breakout_distance_mult: float = 2.0,
        max_hold_minutes: int = 120,
        min_wait_after_range_minutes: int = 15,
        trade_window_minutes: int = 120,
        bar_range_vol_mult: float = 3.0,
        session_close_margin_minutes: int = 5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_minutes  # 1m bars
        self.vol_lookback = vol_lookback
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.min_target_vol_mult = min_target_vol_mult
        self.max_breakout_distance_mult = max_breakout_distance_mult
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.min_wait_after_range_ns = min_wait_after_range_minutes * NS_PER_MINUTE
        self.trade_window_ns = trade_window_minutes * NS_PER_MINUTE
        self.bar_range_vol_mult = bar_range_vol_mult
        self.session_close_margin_ns = session_close_margin_minutes * NS_PER_MINUTE
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.range_ready_ts: int = 0
        self.entry_taken: bool = False

        # Volatility state (true ranges)
        self.last_close: Optional[float] = None
        self.true_ranges: deque[float] = deque(maxlen=2000)

        # Trade state
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state for a new session."""
        self.current_session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * NS_PER_HOUR)
        self.range_high = None
        self.range_low = None
        self.range_ready = False
        self.range_ready_ts = 0
        self.entry_taken = False
        self.entry_time = None
        self.entry_order_id = None
        # Reset last_close so the first bar of a new session does not compute
        # a true range from the previous session's close (avoids overnight gap).
        self.last_close = None

    def _update_opening_range(self, bar) -> None:
        """Accumulate high/low from bars within the opening range window."""
        if self.range_ready:
            return
        if bar.open_ts < self.rth_open_ts:
            return
        opening_range_end = self.rth_open_ts + self.opening_range_bars * NS_PER_MINUTE
        if bar.open_ts >= opening_range_end:
            self.range_ready = True
            self.range_ready_ts = bar.open_ts
            return
        if self.range_high is None:
            self.range_high = bar.high
            self.range_low = bar.low
        else:
            self.range_high = max(self.range_high, bar.high)
            self.range_low = min(self.range_low, bar.low)

    def _atr_volatility(self) -> float:
        """Compute robust volatility as the median true range over the lookback."""
        if len(self.true_ranges) < self.vol_lookback:
            return 0.0
        recent = list(self.true_ranges)[-self.vol_lookback:]
        return statistics.median(recent)

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

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        # Only process our interval
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Update true range (skip first bar of session to avoid overnight gap)
        if self.last_close is not None:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self.last_close),
                abs(bar.low - self.last_close),
            )
            self.true_ranges.append(tr)
        self.last_close = bar.close

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
        if len(self.true_ranges) < self.vol_lookback:
            return

        # Time filter: only trade after min_wait_after_range and within trade_window
        time_since_range_ready = bar.close_ts - self.range_ready_ts
        if time_since_range_ready < self.min_wait_after_range_ns:
            return
        if time_since_range_ready > self.trade_window_ns:
            return

        vol = self._atr_volatility()
        if vol <= 0:
            return

        # Bar range filter: skip extremely volatile bars
        bar_range = bar.high - bar.low
        if bar_range > self.bar_range_vol_mult * vol:
            return

        threshold = self.breakout_vol_mult * vol
        stop_offset = self.stop_vol_mult * vol
        max_breakout_distance = self.max_breakout_distance_mult * vol

        # Fade the breakout: sell on upside breakout, buy on downside breakout
        if bar.close > self.range_high + threshold:
            # Avoid chasing moves that are already too far beyond the range
            if bar.close > self.range_high + max_breakout_distance:
                return
            # Adaptive target: distance to range low, with a minimum
            target_distance = max(bar.close - self.range_low, self.min_target_vol_mult * vol)
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_distance,
                tag="fade_short",
            )
            if order_id is not None:
                self.entry_taken = True
                self.entry_order_id = order_id
        elif bar.close < self.range_low - threshold:
            if bar.close < self.range_low - max_breakout_distance:
                return
            target_distance = max(self.range_high - bar.close, self.min_target_vol_mult * vol)
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_distance,
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
