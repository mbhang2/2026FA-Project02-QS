"""Opening range fade with a trailing stop after breakeven.

This variant builds on #273, the best-scoring strategy so far. It keeps the
proven entry logic, order-flow filters, ATR scaling, first-qualifying-breakout
rule, 150-minute decision window, 100-minute maximum hold, 3.25-hour hard exit
after the regular-hours open, and the breakeven stop after price re-enters the
opening range.

One deliberate change is made: after the protective stop has been moved to
breakeven, it is no longer left there. The strategy now trails the protective
stop in the direction of the fade as the position moves further into profit.
For a short fade, the stop is lowered toward the market once price has moved
favourably by at least 0.75 ATR below the entry; for a long fade, it is raised
symmetrically. Trailing is throttled to at most one modification every five
minutes and only when the new stop would improve the existing stop by at least
0.25 ATR, so the session action cap is never approached by this management.

This is a direct test of the "range wins" account: if a fade entry works, the
profit should be allowed to run, but the first sign of exhaustion should not
round-trip a profitable re-entry all the way back to breakeven. Everything else
is unchanged from #273.
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
        use_trailing_stop: bool = True,
        trail_offset_vol_mult: float = 0.5,
        trail_min_improvement_vol_mult: float = 0.25,
        trail_min_interval_minutes: int = 5,
        hard_exit_hours_after_rth_open: float = 3.25,
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
        self.use_breakeven_stop = use_breakeven_stop
        self.use_trailing_stop = use_trailing_stop
        self.trail_offset_vol_mult = trail_offset_vol_mult
        self.trail_min_improvement_vol_mult = trail_min_improvement_vol_mult
        self.trail_min_interval_ns = trail_min_interval_minutes * NS_PER_MINUTE
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
        self.last_trail_ts: Optional[int] = None

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
        self.breakeven_set = False
        self.last_trail_ts = None
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
        self.breakeven_set = False
        self.last_trail_ts = None

    def _trail_protective_stop(self, ctx, bar) -> None:
        """Trail the protective stop once price has moved favourably.

        The stop is moved only when the new trigger improves the existing one by
        at least `trail_min_improvement_vol_mult` ATR, and no more often than
        `trail_min_interval_ns`. This keeps the number of queued actions per
        session far below the cap while still locking in a portion of the
        range-win move.
        """
        if self.entry_order_id is None or self.entry_direction is None:
            return
        if self.entry_price is None:
            return
        if self.last_trail_ts is not None:
            if ctx.ts - self.last_trail_ts < self.trail_min_interval_ns:
                return

        atr = self._atr()
        if atr <= 0:
            return

        trail_offset = self.trail_offset_vol_mult * atr
        min_improvement = self.trail_min_improvement_vol_mult * atr

        stops = ctx.find_orders(parent_id=self.entry_order_id, order_type="stop")
        for leg in stops:
            current_trigger = getattr(leg, "stop_price", None)
            if current_trigger is None:
                continue

            if self.entry_direction < 0:
                # Short fade: protective stop is a buy stop above the market.
                # Lower it only when price has fallen enough.
                new_trigger = bar.close + trail_offset
                improvement = current_trigger - new_trigger
                if improvement < min_improvement:
                    continue
                if new_trigger >= current_trigger:
                    continue
            else:
                # Long fade: protective stop is a sell stop below the market.
                # Raise it only when price has risen enough.
                new_trigger = bar.close - trail_offset
                improvement = new_trigger - current_trigger
                if improvement < min_improvement:
                    continue
                if new_trigger <= current_trigger:
                    continue

            ctx.modify(leg.id, trigger=new_trigger)
            self.last_trail_ts = ctx.ts
            break

    def _manage_open_position(self, ctx, bar) -> None:
        """Manage time exits and the protective stop."""
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
        if self.entry_order_id is None or self.entry_direction is None or self.entry_price is None:
            return

        # Move the protective stop to breakeven once price has re-entered the
        # opening range. For a short fade, re-entry means price falls back
        # below range_high. For a long fade, re-entry means price rises back
        # above range_low.
        if not self.breakeven_set:
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
                    self.last_trail_ts = ctx.ts
                    break

        # Once the stop is at breakeven, begin trailing it if enabled.
        if self.breakeven_set and self.use_trailing_stop:
            self._trail_protective_stop(ctx, bar)

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
            self.breakeven_set = False
            self.last_trail_ts = None
        elif fill.position == 0:
            # Position closed (stop, target, or manual exit)
            self.entry_time = None
            self.entry_order_id = None
            self.entry_price = None
            self.entry_direction = None
            self.breakeven_set = False
            self.last_trail_ts = None

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
            self.last_trail_ts = None
