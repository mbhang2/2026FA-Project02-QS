"""Opening range fade with range-edge profit target and empty-bar time exits.

This variant builds on #273, the best-scoring strategy so far. It keeps the
proven entry logic: 30-minute RTH opening range, 1m bar close beyond the range
by 0.8 ATR, the two order-flow filters, ATR-scaled stop, at most one entry per
session, 20-minute minimum wait after the range is ready, 150-minute trade
window, 100-minute maximum hold, 11:45 CT hard exit, breakeven stop after
re-entry, and 5-minute session-end margin.

Two deliberate changes are made, both in service of the "range wins" account:

1. The profit target is the opposite edge of the opening range whenever that
   edge is closer than the standard 3.0 ATR target. Concretely, a short fade
   entered above range_high targets range_low, and a long fade entered below
   range_low targets range_high. The target distance is capped at 3.0 ATR so
   very wide ranges keep the original target. This banks the range-return move
   directly instead of waiting for a larger move that may never arrive.

2. setup() subscribes to 1-minute bars with emit_empty=True. The engine
   normally skips empty intervals, so a time-based exit due during a minute
   with no trades can only be executed on the next traded minute. With empty
   bars emitted, the hard exit, maximum hold, and session-end margin are
   evaluated on time. Empty bars are not used for entry signals or for
   ATR/order-flow updates.

Everything else is unchanged from #273.
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
        use_breakeven_stop: bool = True,
        use_range_target: bool = True,
        hard_exit_hours_after_rth_open: float = 3.25,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_minutes
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
        self.use_breakeven_stop = use_breakeven_stop
        self.use_range_target = use_range_target
        self.hard_exit_ns = int(hard_exit_hours_after_rth_open * NS_PER_HOUR)

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
        self.breakeven_set: bool = False

    def setup(self, ctx) -> None:
        # Emit empty 1m bars so time-based exits are not delayed by a minute
        # with no trades.
        ctx.subscribe(self.interval, emit_empty=True)

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
        self.breakeven_set = False
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
            # Mark the range as ready even if the boundary bar is empty.
            if self.range_high is None:
                self.range_high = bar.high
                self.range_low = bar.low
            self.range_ready = True
            self.range_ready_ts = bar.open_ts
            return
        if bar.tick_count == 0:
            # Empty bars carry the previous close forward and do not represent
            # trades inside the opening range.
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
        self.breakeven_set = False

    def _manage_open_position(self, ctx, bar) -> None:
        """Manage time exits and move the protective stop to breakeven."""
        # Hard exit at a fixed time after RTH open
        if ctx.ts >= self.rth_open_ts + self.hard_exit_ns:
            self._exit_position(ctx)
            return
        # Time-based exit
        if self.entry_time is not None and ctx.ts - self.entry_time > self.max_hold_ns:
            self._exit_position(ctx)
            return
        # Session-end exit
        if ctx.session_end - ctx.ts < self.session_close_margin_ns:
            self._exit_position(ctx)
            return

        if not self.use_breakeven_stop:
            return
        if self.breakeven_set:
            return
        if self.entry_order_id is None or self.entry_direction is None or self.entry_price is None:
            return

        # Move the protective stop to breakeven once price has re-entered the
        # opening range. For a short fade, re-entry means price falls back
        # below range_high. For a long fade, re-entry means price rises back
        # above range_low.
        if self.entry_direction > 0:
            if self.range_low is None or not bar.close > self.range_low:
                return
        else:
            if self.range_high is None or not bar.close < self.range_high:
                return

        stops = ctx.find_orders(parent_id=self.entry_order_id, order_type="stop")
        for leg in stops:
            current_trigger = getattr(leg, "stop_price", None)
            if current_trigger is not None and abs(current_trigger - self.entry_price) > 1e-9:
                ctx.modify(leg.id, trigger=self.entry_price)
                self.breakeven_set = True
                break

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        is_empty = bar.tick_count == 0

        # ATR and order-flow features are updated only from real, traded bars.
        if not is_empty:
            self._update_atr(bar)
            self.abs_deltas.append(abs(bar.delta))

        # The opening-range clock still advances on empty bars, but the high/low
        # accumulation ignores them.
        self._update_opening_range(bar)

        # Trading logic only when actionable
        if not ctx.actionable:
            return

        # Manage existing position, using empty bars so time exits are on time.
        if ctx.position:
            self._manage_open_position(ctx, bar)
            return

        # No position, no working orders: consider new entry.
        if ctx.working_order_ids:
            return
        if self.entry_taken:
            return
        if is_empty:
            # Never enter on a synthetic/empty bar that just carries the prior
            # close forward.
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

        # Opening range width filter
        range_width = self.range_high - self.range_low
        if range_width < self.min_range_width_vol_mult * atr:
            return

        # Bar range filter: skip extremely volatile bars
        bar_range = bar.high - bar.low
        if bar_range > self.bar_range_vol_mult * atr:
            return

        threshold = self.breakout_vol_mult * atr
        stop_offset = self.stop_vol_mult * atr
        standard_target_offset = self.target_vol_mult * atr
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

            target_offset = standard_target_offset
            if self.use_range_target:
                # Bank the range-return move: for a short fade, target the low
                # of the opening range, capped at the standard 3 ATR target.
                distance_to_range_low = bar.close - self.range_low
                if distance_to_range_low > 0:
                    target_offset = min(standard_target_offset, distance_to_range_low)

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

            target_offset = standard_target_offset
            if self.use_range_target:
                # Bank the range-return move: for a long fade, target the high
                # of the opening range, capped at the standard 3 ATR target.
                distance_to_range_high = self.range_high - bar.close
                if distance_to_range_high > 0:
                    target_offset = min(standard_target_offset, distance_to_range_high)

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
            self.breakeven_set = False
        elif fill.position == 0:
            # Position closed (stop, target, or manual exit)
            self.entry_time = None
            self.entry_order_id = None
            self.entry_price = None
            self.entry_direction = None
            self.breakeven_set = False

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
            self.breakeven_set = False
