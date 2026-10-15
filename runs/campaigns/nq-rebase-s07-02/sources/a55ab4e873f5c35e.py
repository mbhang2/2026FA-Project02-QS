"""Opening-range departure fade with three separated outcomes.

The question: when price leaves the regular-hours opening range, does the move
hold or does the range win?  This strategy takes the fade side and explicitly
separates the three fates of a departure:

- "move holds": the protective stop is hit.
- "range wins": a one-minute close returns inside the opening range and the
  position is closed at market.
- "undecided": a time horizon expires with neither stop nor range return.

The opening range is the high/low of the first 10 one-minute bars after the
regular-hours open, which is 15.5 hours after the CME Globex session open.
Volatility is a trailing average of recent one-minute true ranges, floored so
the strategy still acts early in the data.  Departure is defined as a
one-minute close beyond the range by a small volatility-scaled buffer.  The
strategy enters opposite the departure: sell above the range, buy below it.

Only one entry is attempted per session.  The protective stop is placed on the
entry fill, so there is no bracket target to conflict with the range-return
exit.  Positions are forced flat five minutes before the session close.
"""

from __future__ import annotations

NS_PER_SECOND = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_SECOND
NS_PER_HOUR = 60 * NS_PER_MINUTE

# CME Globex session opens at 17:00 CT; the regular-hours open is 8:30 CT,
# exactly 15.5 hours later.
REGULAR_OPEN_OFFSET_NS = 15 * NS_PER_HOUR + 30 * NS_PER_MINUTE

STOP_TAG = "protective-stop"


class Strategy:
    def __init__(
        self,
        *,
        opening_minutes: int = 10,
        vol_lookback: int = 20,
        departure_vol_mult: float = 0.25,
        stop_vol_mult: float = 1.25,
        min_vol: float = 0.25,
        min_stop: float = 0.5,
        max_hold_minutes: int = 60,
        session_end_margin_minutes: int = 5,
        size: int = 1,
    ):
        self.opening_minutes = opening_minutes
        self.vol_lookback = vol_lookback
        self.departure_vol_mult = departure_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.min_vol = min_vol
        self.min_stop = min_stop
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.session_end_margin_ns = session_end_margin_minutes * NS_PER_MINUTE
        self.size = size

        # Cross-session volatility state.
        self.recent_ranges: list[float] = []

        # Per-session state.
        self.current_session: int | None = None
        self.regular_open_ts = 0
        self.range_end_ts = 0
        self.range_high: float | None = None
        self.range_low: float | None = None
        self.range_collected = False

        # Trade state.
        self.entered_this_session = False
        self.entry_direction = 0  # +1 long fade, -1 short fade, 0 flat
        self.entry_time: int | None = None
        self.entry_stop_dist = 0.0
        self.stop_placed = False
        self.close_pending = False

    def setup(self, ctx) -> None:
        # One-minute bars, including empty intervals so time exits and the
        # session-end flatten remain reliable even in quiet minutes.
        ctx.subscribe("1m", emit_empty=True)

    def _reset_session(self, ctx) -> None:
        self.current_session = ctx.session
        self.regular_open_ts = ctx.session_start + REGULAR_OPEN_OFFSET_NS
        self.range_end_ts = self.regular_open_ts + self.opening_minutes * NS_PER_MINUTE
        self.range_high = None
        self.range_low = None
        self.range_collected = False
        self.entered_this_session = False
        self.entry_direction = 0
        self.entry_time = None
        self.entry_stop_dist = 0.0
        self.stop_placed = False
        self.close_pending = False

    def _update_volatility(self, bar) -> None:
        """Accumulate one-minute true ranges for the trailing volatility estimate."""
        if bar.tick_count <= 0:
            return
        rng = bar.high - bar.low
        if rng <= 0:
            return
        self.recent_ranges.append(rng)
        if len(self.recent_ranges) > self.vol_lookback:
            self.recent_ranges.pop(0)

    def _current_volatility(self) -> float:
        """Average recent one-minute range, floored by min_vol."""
        recent = self.recent_ranges[-self.vol_lookback:]
        if not recent:
            return self.min_vol
        return max(sum(recent) / len(recent), self.min_vol)

    def _place_entry(self, ctx, direction: int) -> None:
        """Enter a market order to fade the departure."""
        vol = self._current_volatility()
        stop_dist = max(self.stop_vol_mult * vol, self.min_stop)

        if direction > 0:
            order_id = ctx.buy(size=self.size, tag="orb-fade-long")
        else:
            order_id = ctx.sell(size=self.size, tag="orb-fade-short")

        if order_id is None:
            return

        self.entered_this_session = True
        self.entry_direction = direction
        self.entry_time = None
        self.entry_stop_dist = stop_dist
        self.stop_placed = False
        self.close_pending = False

    def _place_stop(self, ctx) -> None:
        """Place a protective stop after the entry fill."""
        direction = self.entry_direction
        if direction > 0:  # long fade
            trigger = ctx.average_entry_price - self.entry_stop_dist
            ctx.sell(
                size=self.size,
                type="stop",
                trigger=trigger,
                reduce_only=True,
                tag=STOP_TAG,
            )
        elif direction < 0:  # short fade
            trigger = ctx.average_entry_price + self.entry_stop_dist
            ctx.buy(
                size=self.size,
                type="stop",
                trigger=trigger,
                reduce_only=True,
                tag=STOP_TAG,
            )

    def _close_position(self, ctx) -> None:
        """Cancel the protective stop and flatten the position."""
        if self.close_pending:
            return

        stops = ctx.find_orders(tag=STOP_TAG)
        for stop_order in stops:
            ctx.cancel(stop_order.id)

        if ctx.position != 0:
            ctx.close()

        self.close_pending = True
        self.entry_direction = 0
        self.entry_time = None
        self.stop_placed = False

    def _manage_open_position(self, ctx, bar) -> None:
        """Manage an open fade position: time exit, session-end exit, or range return."""
        # Undecided: horizon expired.
        if self.entry_time is not None:
            if ctx.ts - self.entry_time >= self.max_hold_ns:
                self._close_position(ctx)
                return

        # Not enough time left: force flat.
        if ctx.ts >= ctx.session_end - self.session_end_margin_ns:
            self._close_position(ctx)
            return

        # Range return is only meaningful on a real trade bar.
        if bar.tick_count <= 0:
            return

        high = self.range_high
        low = self.range_low
        if high is None or low is None:
            return

        if self.entry_direction > 0:  # long fade of a breakdown
            # The range wins when price regains the lower boundary.
            if bar.close >= low:
                self._close_position(ctx)
                return
        elif self.entry_direction < 0:  # short fade of a breakout
            # The range wins when price falls back to the upper boundary.
            if bar.close <= high:
                self._close_position(ctx)
                return

    def on_bars(self, ctx) -> None:
        if "1m" not in ctx.bars:
            return

        bar = ctx.bars["1m"]

        if ctx.session != self.current_session:
            self._reset_session(ctx)

        self._update_volatility(bar)

        if bar.open_ts < self.regular_open_ts:
            return

        if self.close_pending:
            return

        # Manage an open position before looking for a new entry.
        if ctx.position != 0 and self.entry_direction != 0:
            self._manage_open_position(ctx, bar)
            return

        # Accumulate the opening range until the window has passed.
        if not self.range_collected:
            if bar.open_ts < self.range_end_ts:
                if bar.tick_count > 0:
                    if self.range_high is None or bar.high > self.range_high:
                        self.range_high = bar.high
                    if self.range_low is None or bar.low < self.range_low:
                        self.range_low = bar.low
                return

            self.range_collected = True
            if self.range_high is None or self.range_low is None:
                return

        if not ctx.actionable:
            return

        if self.entered_this_session:
            return

        if bar.tick_count == 0:
            return

        high = self.range_high
        low = self.range_low
        if high is None or low is None:
            return

        vol = self._current_volatility()
        buffer = self.departure_vol_mult * vol

        if bar.close > high + buffer:
            # Fade the breakout: short.
            self._place_entry(ctx, -1)
        elif bar.close < low - buffer:
            # Fade the breakdown: long.
            self._place_entry(ctx, 1)

    def on_fill(self, ctx) -> None:
        """Place the protective stop on entry, and reset state when flat."""
        if ctx.position != 0 and self.entry_direction != 0 and not self.stop_placed:
            self.entry_time = ctx.ts
            self.stop_placed = True
            self._place_stop(ctx)
            return

        if ctx.position == 0 and self.entry_direction != 0:
            self.entry_direction = 0
            self.entry_time = None
            self.stop_placed = False
            self.close_pending = False
            return

        if ctx.position == 0 and self.close_pending:
            self.close_pending = False

    def on_lifecycle(self, ctx) -> None:
        pass
