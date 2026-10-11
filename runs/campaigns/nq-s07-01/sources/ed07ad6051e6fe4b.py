"""Opening-range breakout with close-strength direction and a breakeven ratchet.

Keeps the profitable #41 signal exactly: 30-minute RTH opening range,
10% buffer, 5-minute bars, delta confirmation, follow strong-close breakouts,
fade weak-close breakouts, symmetric half-range bracket, one trade per session,
15:00 CT flatten.

The new part is a breakeven ratchet on the protective bracket leg.  Once an
open position has moved in our favor by one quarter of the opening range (half
the initial stop distance for the #41 bracket), the protective stop is modified
to the entry fill price.  This leaves the target leg untouched, so it only
removes downside from trades that have already shown follow-through.  If the
trade subsequently reaches its target the result is unchanged; if it reverses,
the loss is converted to scratch instead of a full half-range loss.  The rule is
fixed, symmetric for long and short, and uses only the current bar and the
position's average entry price.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND, OrderType


class Strategy:
    """Follow strong-close opening-range breakouts, fade weak-close ones, and ratchet stops."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Flatten by 15:00 CT, giving a full hour before the 16:00 CT session close.
    EXIT_DELAY_SEC = 22 * 3600

    # Do not open a new trade in the last 30 minutes before the scheduled exit.
    ENTRY_CUTOFF_DELAY_SEC = EXIT_DELAY_SEC - 30 * 60

    # If a session ends early (half-day), force the exit this long before end.
    SESSION_EXIT_MARGIN_SEC = 10 * 60

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 0.5,
        min_range: float = 2.0,
        breakout_buffer_mult: float = 0.1,
        close_strength: float = 0.6,
        breakeven_trigger_mult: float = 0.25,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.breakout_buffer_mult = breakout_buffer_mult
        self.close_strength = close_strength
        self.breakeven_trigger_mult = breakeven_trigger_mult

        self._session: int = -1
        self._rth_open_ts: int = 0
        self._or_end_ts: int = 0
        self._entry_cutoff_ts: int = 0
        self._exit_ts: int = 0
        self._or_high: Optional[float] = None
        self._or_low: Optional[float] = None
        self._range_complete: bool = False
        self._traded_this_session: bool = False
        self._exit_ordered: bool = False

        # Breakeven-ratchet state.
        self._entry_id: Optional[int] = None
        self._stop_ratcheted: bool = False
        self._breakeven_trigger_dist: float = 0.0

    def setup(self, ctx) -> None:
        # Guarantee a callback every 5 minutes even when no trade occurs, so
        # the range is measured on time and positions can be flattened near
        # the session close even on thin sessions.
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # New session: reset session-local state and precompute fixed times.
        if ctx.session != self._session:
            self._session = ctx.session
            self._rth_open_ts = (
                ctx.session_start + self.RTH_OPEN_DELAY_SEC * NS_PER_SECOND
            )
            self._or_end_ts = (
                self._rth_open_ts + self.OPENING_RANGE_SEC * NS_PER_SECOND
            )
            self._entry_cutoff_ts = (
                ctx.session_start + self.ENTRY_CUTOFF_DELAY_SEC * NS_PER_SECOND
            )
            self._exit_ts = min(
                ctx.session_start + self.EXIT_DELAY_SEC * NS_PER_SECOND,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            self._or_high = None
            self._or_low = None
            self._range_complete = False
            self._traded_this_session = False
            self._exit_ordered = False
            self._entry_id = None
            self._stop_ratcheted = False
            self._breakeven_trigger_dist = 0.0

        # Build the opening range from bars between RTH open and RTH open + 30m.
        if not self._range_complete:
            if bar.close_ts <= self._rth_open_ts:
                return

            if bar.open_ts < self._or_end_ts:
                if bar.tick_count > 0:
                    high = bar.high
                    low = bar.low
                    if self._or_high is None:
                        self._or_high = high
                    elif high > self._or_high:
                        self._or_high = high
                    if self._or_low is None:
                        self._or_low = low
                    elif low < self._or_low:
                        self._or_low = low

                if bar.close_ts >= self._or_end_ts:
                    self._range_complete = True
                # Never act on a bar that is part of the range definition.
                return

            # First bar whose open is at/after the range end: range is complete.
            self._range_complete = True

        if not ctx.actionable:
            return

        # Open-position management: ratchet the protective stop to breakeven
        # after the trade moves favourably by the configured fraction of range.
        if ctx.position:
            self._manage_open_position(ctx)

            # Mandatory flatten before the session ends.  The engine's bracket
            # handles stop/target; this prevents an overnight hold.
            if bar.close_ts >= self._exit_ts and not self._exit_ordered:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True
            return

        # Flat: clear position-tracking state before evaluating entries.
        self._entry_id = None
        self._stop_ratcheted = False
        self._breakeven_trigger_dist = 0.0

        if self._traded_this_session:
            return
        if bar.close_ts >= self._entry_cutoff_ts:
            return
        if bar.tick_count == 0:
            return

        or_high = self._or_high
        or_low = self._or_low
        if or_high is None or or_low is None:
            return

        rng = or_high - or_low
        if rng < self.min_range:
            return

        body = bar.high - bar.low
        if body <= 0:
            return
        close_location = (bar.close - bar.low) / body

        buffer = rng * self.breakout_buffer_mult
        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        order_id = None

        # Above the opening range, with delta confirming the up move.
        if bar.close > or_high + buffer and bar.delta > 0:
            if close_location >= self.close_strength:
                # Strong close: follow the breakout.
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-strong-long",
                )
            else:
                # Weak close after an up break: fade it.
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-weak-fade-short",
                )

        # Below the opening range, with delta confirming the down move.
        elif bar.close < or_low - buffer and bar.delta < 0:
            if close_location <= (1.0 - self.close_strength):
                # Strong close: follow the breakdown.
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-strong-short",
                )
            else:
                # Weak close after a down break: fade it.
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-weak-fade-long",
                )

        if order_id is not None:
            self._traded_this_session = True
            self._entry_id = order_id
            self._stop_ratcheted = False
            self._breakeven_trigger_dist = rng * self.breakeven_trigger_mult

    def _manage_open_position(self, ctx) -> None:
        """Move the bracket stop to breakeven once the trade has moved enough."""
        if self._entry_id is None or self._stop_ratcheted:
            return
        if ctx.position == 0:
            return
        if self._breakeven_trigger_dist <= 0:
            return

        legs = ctx.find_orders(parent_id=self._entry_id)
        stop_order = None
        for leg in legs:
            order_type = leg.order_type
            if order_type == OrderType.STOP or str(order_type).lower() == "stop":
                stop_order = leg
                break

        if stop_order is None:
            return

        entry = ctx.average_entry_price
        last = ctx.last_price

        if ctx.position > 0:
            # Long: raise the sell-stop to entry once price has moved up by the
            # configured amount.
            gain = last - entry
            if gain >= self._breakeven_trigger_dist:
                ctx.modify(stop_order.id, trigger=entry)
                self._stop_ratcheted = True
        else:
            # Short: lower the buy-stop to entry once price has moved down by the
            # configured amount.
            gain = entry - last
            if gain >= self._breakeven_trigger_dist:
                ctx.modify(stop_order.id, trigger=entry)
                self._stop_ratcheted = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
