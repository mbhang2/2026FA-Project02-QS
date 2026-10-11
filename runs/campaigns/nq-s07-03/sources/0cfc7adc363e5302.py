"""Opening range fade with hybrid multi-day ATR and trailing stop after re-entry.

This strategy builds on the best-performing #141 skeleton but makes three
deliberate changes aimed at improving the risk/reward of the fade.

1. **Hybrid volatility lookback.** #141 used only the current session's 1-minute
   bars for ATR, which is noisy and can be distorted by the overnight session.
   #174 used a full day (1440 bars) across sessions and performed worse,
   possibly because the lookback was too long and included stale volatility.
   Here we use a 300-bar (5-hour) rolling ATR that is kept across sessions.
   This provides a multi-day context without being dominated by old data, and
   it is closer to the campaign's request for "trailing realized volatility
   computed from prior days."

2. **Trailing stop after re-entry.** #141 moved the protective stop to
   breakeven once price re-entered the opening range. We extend that: after
   breakeven is set, the stop is trailed by 0.5 ATR as price moves further in
   the favorable direction. This locks in profit when the range continues to
   win, while still giving the trade room to breathe.

3. **Slightly tighter target.** The target is reduced from 3.0 ATR to 2.5 ATR.
   The opening-range fade typically sees price return to the range but not
   travel far beyond it; taking profit a bit earlier may improve the win rate
   without sacrificing too much upside.

All other parameters and filters are unchanged from #141: 30-minute RTH opening
range, 1-minute bars, breakout threshold 0.8 ATR, stop 0.8 ATR, order-flow
filters, one entry per session, 90-minute maximum hold, 150-minute entry
window, and 5-minute session-end margin.
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
        vol_lookback: int = 300,          # 5 hours of 1-minute bars, kept across sessions
        breakout_vol_mult: float = 0.8,
        stop_vol_mult: float = 0.8,
        target_vol_mult: float = 2.5,
        max_hold_minutes: int = 90,
        min_wait_after_range_minutes: int = 20,
        trade_window_minutes: int = 150,
        bar_range_vol_mult: float = 2.0,
        min_range_width_vol_mult: float = 1.0,
        max_breakout_imbalance: float = 0.20,
        max_breakout_delta_abs_mult: float = 2.0,
        session_close_margin_minutes: int = 5,
        size: int = 1,
        use_breakeven_stop: bool = True,
        trail_vol_mult: float = 0.5,
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
        self.trail_vol_mult = trail_vol_mult

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.range_ready_ts: int = 0
        self.entry_taken: bool = False

        # Volatility state (ATR) -- kept across sessions, rolling sum O(1)
        self.last_close: Optional[float] = None
        self.true_ranges: deque[float] = deque(maxlen=vol_lookback)
        self._tr_sum: float = 0.0

        # Order-flow state: absolute delta per bar, reset each session
        self.abs_deltas: deque[float] = deque(maxlen=vol_lookback)
        self._abs_delta_sum: float = 0.0

        # Trade state
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None
        self.entry_price: Optional[float] = None
        self.entry_direction: Optional[int] = None  # +1 long, -1 short
        self.breakeven_set: bool = False
        self.trailing_stop_active: bool = False
        self.current_stop_price: Optional[float] = None
        self.highest_price_since_entry: Optional[float] = None
        self.lowest_price_since_entry: Optional[float] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state for a new session.

        Volatility buffer is intentionally NOT reset; it carries across sessions
        to provide multi-day context. Order-flow buffer IS reset to avoid
        cross-session contamination.
        """
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
        self.trailing_stop_active = False
        self.current_stop_price = None
        self.highest_price_since_entry = None
        self.lowest_price_since_entry = None
        # Reset order-flow history for the new session
        self.abs_deltas.clear()
        self._abs_delta_sum = 0.0

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
        """Update the ATR buffer with the current bar's true range, O(1)."""
        if self.last_close is None:
            # First bar ever: true range is just high - low
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self.last_close),
                abs(bar.low - self.last_close),
            )
        # Maintain rolling sum with fixed-size deque
        if len(self.true_ranges) == self.vol_lookback:
            self._tr_sum -= self.true_ranges[0]
        self.true_ranges.append(tr)
        self._tr_sum += tr
        self.last_close = bar.close

    def _atr(self) -> float:
        """Average true range over the lookback window, O(1)."""
        if len(self.true_ranges) < self.vol_lookback:
            return 0.0
        return self._tr_sum / len(self.true_ranges)

    def _update_abs_delta(self, bar) -> None:
        """Update the absolute delta buffer, O(1)."""
        val = abs(bar.delta)
        if len(self.abs_deltas) == self.vol_lookback:
            self._abs_delta_sum -= self.abs_deltas[0]
        self.abs_deltas.append(val)
        self._abs_delta_sum += val

    def _avg_abs_delta(self) -> float:
        """Average absolute signed volume over the lookback window, O(1)."""
        if len(self.abs_deltas) < self.vol_lookback:
            return 0.0
        return self._abs_delta_sum / len(self.abs_deltas)

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
        self.trailing_stop_active = False
        self.current_stop_price = None
        self.highest_price_since_entry = None
        self.lowest_price_since_entry = None

    def _manage_open_position(self, ctx, bar) -> None:
        """Manage time exits, breakeven stop, and trailing stop."""
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

        # Update extreme prices for trailing
        if self.entry_direction > 0:  # long
            if self.highest_price_since_entry is None or bar.high > self.highest_price_since_entry:
                self.highest_price_since_entry = bar.high
        else:  # short
            if self.lowest_price_since_entry is None or bar.low < self.lowest_price_since_entry:
                self.lowest_price_since_entry = bar.low

        # Check if we should set breakeven (first re-entry into range)
        if not self.breakeven_set:
            if self.entry_direction > 0:
                if self.range_low is None or not bar.close > self.range_low:
                    return
            else:
                if self.range_high is None or not bar.close < self.range_high:
                    return
            # Set breakeven
            stops = ctx.find_orders(parent_id=self.entry_order_id, order_type="stop")
            for leg in stops:
                current_trigger = getattr(leg, "stop_price", None)
                if current_trigger is not None and abs(current_trigger - self.entry_price) > 1e-9:
                    ctx.modify(leg.id, trigger=self.entry_price)
                    self.breakeven_set = True
                    self.current_stop_price = self.entry_price
                    self.trailing_stop_active = True
                    break
            return

        # Trailing stop after breakeven
        if self.trailing_stop_active and self.current_stop_price is not None:
            atr = self._atr()
            if atr <= 0:
                return
            trail_distance = self.trail_vol_mult * atr
            new_stop = None
            if self.entry_direction > 0:  # long: trail upward
                if self.highest_price_since_entry is not None:
                    candidate = self.highest_price_since_entry - trail_distance
                    if candidate > self.current_stop_price:
                        new_stop = candidate
            else:  # short: trail downward
                if self.lowest_price_since_entry is not None:
                    candidate = self.lowest_price_since_entry + trail_distance
                    if candidate < self.current_stop_price:
                        new_stop = candidate
            if new_stop is not None:
                stops = ctx.find_orders(parent_id=self.entry_order_id, order_type="stop")
                for leg in stops:
                    ctx.modify(leg.id, trigger=new_stop)
                    self.current_stop_price = new_stop
                    break

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        # Only process our interval
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Update ATR and order-flow (always, even during warmup)
        self._update_atr(bar)
        self._update_abs_delta(bar)

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
            self.trailing_stop_active = False
            self.current_stop_price = None
            self.highest_price_since_entry = None
            self.lowest_price_since_entry = None
        elif fill.position == 0:
            # Position closed (stop, target, or manual exit)
            self.entry_time = None
            self.entry_order_id = None
            self.entry_price = None
            self.entry_direction = None
            self.breakeven_set = False
            self.trailing_stop_active = False
            self.current_stop_price = None
            self.highest_price_since_entry = None
            self.lowest_price_since_entry = None

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
            self.trailing_stop_active = False
            self.current_stop_price = None
            self.highest_price_since_entry = None
            self.lowest_price_since_entry = None
