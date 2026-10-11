"""Opening range fade with trailing stop after re-entry.

This variant builds on #141, the best so far, by replacing the fixed breakeven
stop with a trailing stop that activates once price re-enters the opening range.
The thesis remains that departures from the opening range are overextensions and
price returns inside. Once price is back inside, the original stop distance is no
longer the relevant risk; instead of locking in exactly zero, we trail the stop
behind price using a fraction of ATR. This lets winners run further while still
protecting against a reversal that would turn a profitable fade into a loss.

Everything else is unchanged from #141: 30-minute RTH opening range, 1m bar
close beyond the range by 0.8 ATR, the two order-flow filters, ATR-scaled
bracket, at most one entry per session, 100-minute maximum hold, 5-minute
session-end margin, and the extended 150-minute decision window.
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
        vol_lookback: int = 150,
        breakout_vol_mult: float = 0.8,
        stop_vol_mult: float = 0.8,
        target_vol_mult: float = 3.0,
        max_hold_minutes: int = 100,
        min_wait_after_range_minutes: int = 20,
        trade_window_minutes: int = 150,
        bar_range_vol_mult: float = 2.0,
        min_range_width_vol_mult: float = 1.0,
        max_breakout_imbalance: float = 0.20,
        max_breakout_delta_abs_mult: float = 2.0,
        session_close_margin_minutes: int = 5,
        size: int = 1,
        trail_atr_mult: float = 0.5,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_minutes  # 1m bars
        self.vol_lookback = vol_lookback
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.min_wait_after_range_ns = min_wait_after_range_minutes * NS_PER_MINUTE
        self.trade_window_ns = trade_window_minutes * NS_PER_MINUTE
        self.bar_range_vol_mult = bar_range_vol_mult
        self.min_range_width_vol_mult = min_range_width_vol_mult
        self.max_breakout_imbalance = max_breakout_imbalance
        self.max_breakout_delta_abs_mult = max_breakout_delta_abs_mult
        self.session_close_margin_ns = session_close_margin_minutes * NS_PER_MINUTE
        self.size = size
        self.trail_atr_mult = trail_atr_mult

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.range_ready_ts: int = 0
        self.entry_taken: bool = False

        # Volatility state (ATR)
        self.last_close: Optional[float] = None
        self.true_ranges: deque[float] = deque(maxlen=2000)

        # Order-flow state: absolute delta per bar, for the second filter
        self.abs_deltas: deque[float] = deque(maxlen=2000)

        # Trade state
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None
        self.entry_price: Optional[float] = None
        self.entry_direction: Optional[int] = None  # +1 long, -1 short
        self.trailing_active: bool = False
        self.highest_since_entry: Optional[float] = None
        self.lowest_since_entry: Optional[float] = None

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
        self.entry_price = None
        self.entry_direction = None
        self.trailing_active = False
        self.highest_since_entry = None
        self.lowest_since_entry = None
        # Reset volatility so the first bar of a new session does not compute
        # a true range from the previous session's close (avoids overnight gap).
        self.last_close = None
        self.true_ranges.clear()
        # Clear order-flow history for the new session as well.
        self.abs_deltas.clear()

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

    def _update_atr(self, bar) -> None:
        """Update the ATR buffer with the current bar's true range."""
        if self.last_close is None:
            # First bar of session: true range is just high - low
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self.last_close),
                abs(bar.low - self.last_close),
            )
        self.true_ranges.append(tr)
        self.last_close = bar.close

    def _atr(self) -> float:
        """Compute average true range over the lookback window."""
        if len(self.true_ranges) < self.vol_lookback:
            return 0.0
        recent = list(self.true_ranges)[-self.vol_lookback:]
        return sum(recent) / len(recent)

    def _avg_abs_delta(self) -> float:
        """Average absolute signed volume over the lookback window."""
        if len(self.abs_deltas) < self.vol_lookback:
            return 0.0
        recent = list(self.abs_deltas)[-self.vol_lookback:]
        return sum(recent) / len(recent)

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
        self.entry_price = None
        self.entry_direction = None
        self.trailing_active = False
        self.highest_since_entry = None
        self.lowest_since_entry = None

    def _manage_open_position(self, ctx, bar) -> None:
        """Manage time exits and trailing stop."""
        # Time-based exit
        if self.entry_time is not None and ctx.ts - self.entry_time > self.max_hold_ns:
            self._exit_position(ctx)
            return
        # Session-end exit
        if ctx.session_end - ctx.ts < self.session_close_margin_ns:
            self._exit_position(ctx)
            return

        if self.entry_order_id is None or self.entry_direction is None or self.entry_price is None:
            return

        # Update highest/lowest since entry
        if self.entry_direction > 0:  # long
            if self.highest_since_entry is None or bar.high > self.highest_since_entry:
                self.highest_since_entry = bar.high
        else:  # short
            if self.lowest_since_entry is None or bar.low < self.lowest_since_entry:
                self.lowest_since_entry = bar.low

        # Check if price has re-entered the opening range to activate trailing
        if not self.trailing_active:
            if self.entry_direction > 0:
                if self.range_low is None or not bar.close > self.range_low:
                    return
            else:
                if self.range_high is None or not bar.close < self.range_high:
                    return
            # Activate trailing: set initial stop to entry price
            self.trailing_active = True
            stops = ctx.find_orders(parent_id=self.entry_order_id, order_type="stop")
            for leg in stops:
                current_trigger = getattr(leg, "stop_price", None)
                if current_trigger is not None and abs(current_trigger - self.entry_price) > 1e-9:
                    ctx.modify(leg.id, trigger=self.entry_price)
                    break
            return

        # Trailing stop logic (only after activation)
        atr = self._atr()
        if atr <= 0:
            return
        trail_distance = self.trail_atr_mult * atr

        # Use local variables to satisfy mypy and avoid Optional arithmetic
        entry_price = self.entry_price
        if self.entry_direction > 0:  # long
            highest = self.highest_since_entry
            if highest is None:
                return
            new_stop = max(entry_price, highest - trail_distance)
        else:  # short
            lowest = self.lowest_since_entry
            if lowest is None:
                return
            new_stop = min(entry_price, lowest + trail_distance)

        stops = ctx.find_orders(parent_id=self.entry_order_id, order_type="stop")
        for leg in stops:
            current_trigger = getattr(leg, "stop_price", None)
            if current_trigger is None:
                continue
            # Only move stop if it improves (for long: higher; for short: lower)
            if self.entry_direction > 0 and new_stop > current_trigger + 1e-9:
                ctx.modify(leg.id, trigger=new_stop)
                break
            elif self.entry_direction < 0 and new_stop < current_trigger - 1e-9:
                ctx.modify(leg.id, trigger=new_stop)
                break

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        # Only process our interval
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Update ATR (always, even during warmup)
        self._update_atr(bar)

        # Keep order-flow history, also always, even during warmup
        self.abs_deltas.append(abs(bar.delta))

        # Update opening range
        self._update_opening_range(bar)

        # Trading logic only when actionable
        if not ctx.actionable:
            return

        # Manage existing position
        if ctx.position:
            self._manage_open_position(ctx, bar)
            return  # let bracket/time exits manage

        # No position, no working orders: consider new entry
        if ctx.working_order_ids:
            return
        if self.entry_taken:
            return
        if not self.range_ready or self.range_high is None or self.range_low is None:
            return
        if len(self.true_ranges) < self.vol_lookback:
            return
        if len(self.abs_deltas) < self.vol_lookback:
            return

        # Time filter: only trade after min_wait_after_range and within trade_window
        time_since_range_ready = bar.close_ts - self.range_ready_ts
        if time_since_range_ready < self.min_wait_after_range_ns:
            return
        if time_since_range_ready > self.trade_window_ns:
            return

        atr = self._atr()
        if atr <= 0:
            return

        # Opening range width filter: require range to be at least min_range_width_vol_mult * ATR
        range_width = self.range_high - self.range_low
        if range_width < self.min_range_width_vol_mult * atr:
            return

        # Bar range filter: skip extremely volatile bars
        bar_range = bar.high - bar.low
        if bar_range > self.bar_range_vol_mult * atr:
            return

        threshold = self.breakout_vol_mult * atr
        stop_offset = self.stop_vol_mult * atr
        target_offset = self.target_vol_mult * atr
        volume = max(1, bar.volume)
        avg_abs_delta = self._avg_abs_delta()

        # Fade the breakout: sell on upside breakout, buy on downside breakout.
        # Skip breakouts whose signed volume is strongly aligned with the move,
        # either as a fraction of the breakout bar's own volume or as an
        # unusually large absolute flow relative to recent bars.
        if bar.close > self.range_high + threshold:
            if bar.delta > self.max_breakout_imbalance * volume:
                return
            if (
                avg_abs_delta > 0
                and bar.delta > self.max_breakout_delta_abs_mult * avg_abs_delta
            ):
                return
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
            if bar.delta < -self.max_breakout_imbalance * volume:
                return
            if (
                avg_abs_delta > 0
                and bar.delta < -self.max_breakout_delta_abs_mult * avg_abs_delta
            ):
                return
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
            self.entry_price = fill.price
            self.entry_direction = 1 if fill.position > 0 else -1
            self.trailing_active = False
            self.highest_since_entry = fill.price
            self.lowest_since_entry = fill.price
        elif fill.position == 0:
            # Position closed (stop, target, or manual exit)
            self.entry_time = None
            self.entry_order_id = None
            self.entry_price = None
            self.entry_direction = None
            self.trailing_active = False
            self.highest_since_entry = None
            self.lowest_since_entry = None

    def on_lifecycle(self, ctx) -> None:
        event = ctx.event
        if event.order_id == self.entry_order_id and event.kind in (
            "day_expired",
            "cancel_confirmed",
            "order_rejected",
        ):
            self.entry_order_id = None
            self.entry_time = None
            self.entry_price = None
            self.entry_direction = None
            self.trailing_active = False
            self.highest_since_entry = None
            self.lowest_since_entry = None
