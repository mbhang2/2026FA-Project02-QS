"""Opening range breakout with 5-minute bars, ATR-based threshold, and time window.

Investigates whether a move out of the opening range holds. The opening range is
the first 30 minutes of the regular-hours session (six 5-minute bars). Volatility
is the average true range of the last 20 5-minute bars. A breakout occurs when a
5-minute bar closes beyond the range by 0.5 * ATR. Trades are taken only within
the first 2 hours after the RTH open. A volume filter requires the breakout bar's
volume to exceed 1.5 times the average volume of the opening range bars. Positions
are protected by a bracket with stop and target offsets of 1.0 and 2.0 times ATR,
respectively. Positions are closed 5 minutes before session end.
"""

from __future__ import annotations

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
        target_mult: float = 2.0,
        trade_window_minutes: int = 120,
        volume_mult: float = 1.5,
        exit_margin_minutes: int = 5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_minutes // 5  # assumes 5m bars
        self.atr_lookback = atr_lookback
        self.breakout_mult = breakout_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.trade_window_bars = trade_window_minutes // 5
        self.volume_mult = volume_mult
        self.exit_margin_ns = exit_margin_minutes * NS_PER_MINUTE
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.bars_since_rth_open: int = 0
        self.opening_range_high: float = float("-inf")
        self.opening_range_low: float = float("inf")
        self.opening_range_volume_sum: int = 0
        self.opening_range_bar_count: int = 0
        self.opening_range_ready: bool = False

        # Trade state
        self.entry_order_id: Optional[int] = None
        self.entry_pending: bool = False
        self.trade_active: bool = False
        self.entry_time: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state for a new session."""
        self.current_session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * NS_PER_HOUR)
        self.bars_since_rth_open = 0
        self.opening_range_high = float("-inf")
        self.opening_range_low = float("inf")
        self.opening_range_volume_sum = 0
        self.opening_range_bar_count = 0
        self.opening_range_ready = False
        self.entry_order_id = None
        self.entry_pending = False
        self.trade_active = False
        self.entry_time = 0

    def _compute_atr(self, ctx) -> Optional[float]:
        """Average true range of the last `atr_lookback` 5-minute bars."""
        history = ctx.history(self.interval)
        if len(history) < self.atr_lookback:
            return None
        recent = history[-self.atr_lookback:]
        ranges = [bar.high - bar.low for bar in recent]
        if not ranges:
            return None
        return sum(ranges) / len(ranges)

    def _update_opening_range(self, bar) -> None:
        """Accumulate high/low/volume from bars within the opening range window."""
        if self.opening_range_ready:
            return
        if bar.open_ts < self.rth_open_ts:
            return
        if bar.open_ts >= self.rth_open_ts + self.opening_range_bars * 5 * NS_PER_MINUTE:
            self.opening_range_ready = True
            return
        self.opening_range_high = max(self.opening_range_high, bar.high)
        self.opening_range_low = min(self.opening_range_low, bar.low)
        self.opening_range_volume_sum += bar.volume
        self.opening_range_bar_count += 1

    def _exit_position(self, ctx) -> None:
        """Cancel bracket legs and flatten the position."""
        if self.entry_order_id is not None:
            legs = ctx.find_orders(parent_id=self.entry_order_id)
            for leg in legs:
                ctx.cancel(leg.id)
        ctx.close()
        self.entry_order_id = None
        self.entry_pending = False
        self.trade_active = False
        self.entry_time = 0

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        # Only process our interval
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Update opening range
        self._update_opening_range(bar)

        # Track bars since RTH open
        if bar.close_ts >= self.rth_open_ts:
            if self.bars_since_rth_open == 0:
                self.bars_since_rth_open = 1
            else:
                self.bars_since_rth_open += 1

        # Trading logic only when actionable
        if not ctx.actionable:
            return

        # Manage existing position
        if ctx.position or ctx.working_order_ids:
            if self.trade_active and ctx.position != 0:
                # Time-based exit: close near session end
                if ctx.session_end - ctx.ts < self.exit_margin_ns:
                    self._exit_position(ctx)
                    return
                # Max hold: close after trade window (optional, but we already restrict entry)
                # We could add a max hold, but bracket handles exits.
            return

        # No position, no working orders: consider new entry
        if not self.opening_range_ready:
            return
        if self.bars_since_rth_open > self.trade_window_bars:
            return

        # Compute ATR
        atr = self._compute_atr(ctx)
        if atr is None or atr <= 0:
            return

        # Volume filter: require breakout bar volume > volume_mult * average opening range volume
        if self.opening_range_bar_count > 0:
            avg_volume = self.opening_range_volume_sum / self.opening_range_bar_count
            if bar.volume < self.volume_mult * avg_volume:
                return

        threshold = self.breakout_mult * atr
        if bar.close > self.opening_range_high + threshold:
            self.entry_order_id = ctx.buy(
                size=self.size,
                stop_offset=self.stop_mult * atr,
                target_offset=self.target_mult * atr,
                tag="orb_breakout_long",
            )
            if self.entry_order_id is not None:
                self.entry_pending = True
        elif bar.close < self.opening_range_low - threshold:
            self.entry_order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_mult * atr,
                target_offset=self.target_mult * atr,
                tag="orb_breakout_short",
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
