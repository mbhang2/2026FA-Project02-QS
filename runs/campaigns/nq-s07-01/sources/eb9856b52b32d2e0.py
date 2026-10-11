"""Opening-range breakout with a breakeven trail after a half-range move.

The campaign asks whether a move out of the regular-hours opening range holds
or the range wins.  The best prior strategy (#23) took the first 5-minute close
beyond a 30-minute RTH opening range, buffered by 10% of the range height, with
signed order-flow confirmation (`bar.delta`), and a symmetric half-range
bracket.  It scored +0.1290.

This proposal keeps #23's entry exactly, but after the entry is filled the
protective stop is moved to breakeven once price has moved in our favour by half
the initial stop distance.  The claim is that a breakout which travels half the
range before failing should not be allowed to become a full loser.  Locking in a
scratch trade on that subset should improve the average outcome without changing
which sessions are traded or how the entry is selected.

The engine's bracket still arms the initial stop and target.  We locate the
bracket's stop leg by its `parent_id` (the entry order id) and modify its
trigger price once the breakeven threshold is reached.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND, OrderType


class Strategy:
    """Trade the first delta-confirmed opening-range breakout that clears the
    range by a 10% buffer, then trail the stop to breakeven after a half-range
    favourable move."""

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
        breakeven_trigger_mult: float = 0.5,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.breakout_buffer_mult = breakout_buffer_mult

        # When price moves this fraction of the initial stop in our favour,
        # move the protective stop to the entry price.
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

        # Trailing-stop state for the open position.
        self._entry_id: Optional[int] = None
        self._entry_price: Optional[float] = None
        self._stop_dist: float = 0.0
        self._breakeven_done: bool = False

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
            self._entry_price = None
            self._stop_dist = 0.0
            self._breakeven_done = False

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

        # If we have an open position, manage the trailing stop before any
        # mandatory flatten.
        if ctx.position:
            self._maybe_move_stop_to_breakeven(ctx)

            if bar.close_ts >= self._exit_ts and not self._exit_ordered:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True
            return

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

        buffer = rng * self.breakout_buffer_mult
        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        order_id = None

        # Trade the first close outside the range by at least the buffer whose
        # signed order flow confirms the direction of the break.
        if bar.close > or_high + buffer and bar.delta > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-buffer-long",
            )
        elif bar.close < or_low - buffer and bar.delta < 0:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-buffer-short",
            )

        if order_id is not None:
            self._traded_this_session = True
            self._entry_id = order_id
            self._stop_dist = stop_dist
            self._breakeven_done = False

    def _maybe_move_stop_to_breakeven(self, ctx) -> None:
        """Move the bracket's stop leg to breakeven after a favourable move.

        The bracket stop is found by parent_id rather than by an id held across
        callbacks, so a DAY expiry elsewhere cannot make this logic act on a
        stale id.
        """
        if self._breakeven_done or self._entry_id is None or self._entry_price is None:
            return

        if ctx.position > 0:
            if ctx.last_price >= self._entry_price + self.breakeven_trigger_mult * self._stop_dist:
                stops = ctx.find_orders(
                    parent_id=self._entry_id, order_type=OrderType.STOP
                )
                if stops:
                    ctx.modify(stops[0].id, trigger=self._entry_price)
                    self._breakeven_done = True

        elif ctx.position < 0:
            if ctx.last_price <= self._entry_price - self.breakeven_trigger_mult * self._stop_dist:
                stops = ctx.find_orders(
                    parent_id=self._entry_id, order_type=OrderType.STOP
                )
                if stops:
                    ctx.modify(stops[0].id, trigger=self._entry_price)
                    self._breakeven_done = True

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        # A fill that brings position back to zero is a stop/target/close fill.
        if fill.position == 0:
            self._entry_id = None
            self._entry_price = None
            self._stop_dist = 0.0
            self._breakeven_done = False
            return

        # Opening fill: record the actual fill price for trailing logic.
        self._entry_price = fill.price
        self._breakeven_done = False

    def on_lifecycle(self, ctx) -> None:
        pass
