"""Opening range breakout with volatility-scaled buffer and time-based exit.

The strategy defines the opening range as the first 30 minutes of the regular
session (starting at 8:30 CT, which is 15.5 hours after the Globex session open
at 17:00 CT). It computes a trailing ATR from the previous 20 daily bars to
scale the breakout buffer and the stop/target distances. When price closes
beyond the range by 0.5 * ATR, it enters in the breakout direction with a
bracket (stop = 1.0 * ATR, target = 2.0 * ATR). If neither stop nor target is
hit within 60 minutes, the position is closed at market and counted as
undecided. The strategy acts only once per session and only during regular
hours.
"""

from __future__ import annotations

from typing import Optional

# Time constants (nanoseconds)
NS_PER_SECOND = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_SECOND
NS_PER_HOUR = 60 * NS_PER_MINUTE


class Strategy:
    """Opening range breakout strategy."""

    def __init__(
        self,
        *,
        range_minutes: int = 30,
        buffer_mult: float = 0.5,
        stop_mult: float = 1.0,
        target_mult: float = 2.0,
        max_hold_minutes: int = 60,
        atr_lookback: int = 20,
        session_end_margin_minutes: int = 5,
    ) -> None:
        self.range_minutes = range_minutes
        self.buffer_mult = buffer_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_hold_minutes = max_hold_minutes
        self.atr_lookback = atr_lookback
        self.session_end_margin_ns = session_end_margin_minutes * NS_PER_MINUTE

        # Per-session state
        self.regular_open_offset_ns: int = 0  # set in setup()
        self.regular_open_ts: Optional[int] = None
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_collected: bool = False
        self.range_bars_count: int = 0
        self.entered_this_session: bool = False
        self.session_done: bool = False
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None
        self.atr: float = 0.0

        # Daily ranges for ATR calculation
        self.daily_ranges: list[float] = []

    def setup(self, ctx) -> None:
        """Subscribe to 1-minute bars for intraday decisions and 1-day bars for ATR."""
        ctx.subscribe("1m")
        ctx.subscribe("1d")
        # Regular session open offset from Globex session start (17:00 CT to 8:30 CT)
        self.regular_open_offset_ns = int(15.5 * NS_PER_HOUR)

    def _compute_atr(self, ctx) -> None:
        """Compute ATR from the last `atr_lookback` daily bars."""
        daily = ctx.history("1d")
        if len(daily) < self.atr_lookback:
            self.atr = 0.0
            return
        recent = daily.last(self.atr_lookback)
        ranges = [bar.high - bar.low for bar in recent if bar.high > bar.low]
        if not ranges:
            self.atr = 0.0
        else:
            self.atr = sum(ranges) / len(ranges)

    def _reset_session_state(self, ctx) -> None:
        """Reset all per-session variables at session open."""
        self.regular_open_ts = ctx.session_start + self.regular_open_offset_ns
        self.range_high = None
        self.range_low = None
        self.range_collected = False
        self.range_bars_count = 0
        self.entered_this_session = False
        self.session_done = False
        self.entry_time = None
        self.entry_order_id = None
        self._compute_atr(ctx)

    def _close_position(self, ctx) -> None:
        """Cancel bracket legs and close the position."""
        if self.entry_order_id is not None:
            legs = ctx.find_orders(parent_id=self.entry_order_id)
            for leg in legs:
                ctx.cancel(leg.id)
        if ctx.position:
            ctx.close()
        self.session_done = True
        self.entry_time = None
        self.entry_order_id = None

    def on_bars(self, ctx) -> None:
        """Handle bar closes for 1m and 1d subscriptions."""
        if "1d" in ctx.bars:
            daily_bar = ctx.bars["1d"]
            if daily_bar.high > daily_bar.low:
                self.daily_ranges.append(daily_bar.high - daily_bar.low)
                if len(self.daily_ranges) > self.atr_lookback:
                    self.daily_ranges.pop(0)
            return

        if "1m" not in ctx.bars:
            return

        bar = ctx.bars["1m"]

        if self.regular_open_ts is None or bar.open_ts < self.regular_open_ts:
            return

        if self.session_done:
            return

        if ctx.position:
            if self.entry_time is not None:
                hold_ns = ctx.ts - self.entry_time
                if hold_ns > self.max_hold_minutes * NS_PER_MINUTE:
                    self._close_position(ctx)
                    return
            if ctx.session_end - ctx.ts < self.session_end_margin_ns:
                self._close_position(ctx)
                return
            return

        if self.entered_this_session:
            return

        if not self.range_collected:
            if self.range_bars_count < self.range_minutes:
                if self.range_high is None or bar.high > self.range_high:
                    self.range_high = bar.high
                if self.range_low is None or bar.low < self.range_low:
                    self.range_low = bar.low
                self.range_bars_count += 1
                if self.range_bars_count >= self.range_minutes:
                    self.range_collected = True
            return

        if self.atr <= 0:
            return

        # Guard against None for mypy; range_collected implies these are set.
        if self.range_high is None or self.range_low is None:
            return

        buffer = self.buffer_mult * self.atr
        stop_dist = self.stop_mult * self.atr
        target_dist = self.target_mult * self.atr

        if bar.close > self.range_high + buffer:
            order_id = ctx.buy(
                size=1,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="entry",
            )
            if order_id is not None:
                self.entry_order_id = order_id
                self.entered_this_session = True
        elif bar.close < self.range_low - buffer:
            order_id = ctx.sell(
                size=1,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="entry",
            )
            if order_id is not None:
                self.entry_order_id = order_id
                self.entered_this_session = True

    def on_fill(self, ctx) -> None:
        """Handle fills: set entry time on entry fill, detect bracket leg fills."""
        fill = ctx.fill
        if fill.order_id == self.entry_order_id and fill.position != 0:
            self.entry_time = ctx.ts
        elif fill.position == 0 and self.entry_order_id is not None:
            self.session_done = True
            self.entry_time = None
            self.entry_order_id = None

    def on_lifecycle(self, ctx) -> None:
        """Handle session open to reset state and compute ATR."""
        if ctx.event.kind == "session_open":
            self._reset_session_state(ctx)
