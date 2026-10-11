"""Opening-range breakout with order-flow confirmation and a movable stop.

The campaign asks whether a move out of the regular-hours opening range holds or
the range wins.  The current best strategy (#11) took the *move holds* side,
entered on the first 5-minute close outside the opening range whose `bar.delta`
agreed with the direction of the break, and used a symmetric half-range bracket.
It scored +0.0438.

This proposal keeps that structure and adds one change to the exit side: once a
trade has moved in our favour by half the initial stop distance, the protective
stop is moved to breakeven.  The initial bracket is still armed by the engine at
the instant the entry fills, so the position is never unprotected; we then use
`ctx.find_orders(parent_id=entry_id)` to locate the working stop leg and modify
it.  This preserves the favourable one-trade-per-session behaviour while cutting
some of the losses on breakouts that move part-way and then fail.

Opening range:
    High and low of the first 30 minutes of regular-hours trading
    (`08:30 CT` to `09:00 CT`), measured on 5-minute bars.  The regular-hours
    open is 15.5 hours after the CME Globex session open (`17:00 CT`); with bars
    anchored to the session open this is a fixed bar offset and involves no
    lookahead.

Signal:
    After the range is known, the first completed 5-minute bar that closes above
    the range high with `bar.delta > 0` is bought.  The first completed bar that
    closes below the range low with `bar.delta < 0` is sold.  One trade per
    session.

Exit:
    Entry is protected immediately by the engine's bracket, with stop and target
    offsets both equal to half the opening-range height.  Once price moves half
    the initial stop distance in our favour, the stop is moved to breakeven.
    Any open position is flattened by `15:00 CT`, or earlier on short sessions,
    because the engine does not flatten positions automatically.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND, OrderType


class Strategy:
    """Trade the first order-flow-confirmed opening-range breakout, then trail the stop."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Flatten by 15:00 CT, giving a full hour before the 16:00 CT session close.
    EXIT_DELAY_SEC = 22 * 3600

    # If a session ends early (half-day), force the exit this long before end.
    SESSION_EXIT_MARGIN_SEC = 10 * 60

    # No new entry in the last 30 minutes before the effective exit.
    ENTRY_CUTOFF_BEFORE_EXIT_SEC = 30 * 60

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 0.5,
        min_range: float = 2.0,
        breakeven_trigger_mult: float = 0.5,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
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

        # State for the movable stop.
        self._entry_id: Optional[int] = None
        self._stop_dist: float = 0.0
        self._breakeven_armed: bool = False

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
            self._rth_open_ts = ctx.session_start + self.RTH_OPEN_DELAY_SEC * NS_PER_SECOND
            self._or_end_ts = self._rth_open_ts + self.OPENING_RANGE_SEC * NS_PER_SECOND

            scheduled_exit_ts = ctx.session_start + self.EXIT_DELAY_SEC * NS_PER_SECOND
            self._exit_ts = min(
                scheduled_exit_ts,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            self._entry_cutoff_ts = (
                self._exit_ts - self.ENTRY_CUTOFF_BEFORE_EXIT_SEC * NS_PER_SECOND
            )

            self._or_high = None
            self._or_low = None
            self._range_complete = False
            self._traded_this_session = False
            self._exit_ordered = False
            self._entry_id = None
            self._stop_dist = 0.0
            self._breakeven_armed = False

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

        # Mandatory flatten before the session ends.  The engine's bracket
        # handles stop/target; this prevents an overnight hold.
        if ctx.position:
            if bar.close_ts >= self._exit_ts and not self._exit_ordered:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True
                return

            self._manage_open_position(ctx, bar)
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

        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        order_id = None

        # Trade the first close outside the range whose signed order flow
        # confirms the direction of the break.  A delta in the opposite
        # direction suggests the move is being absorbed and is more likely to
        # fail.
        if bar.close > or_high and bar.delta > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-long",
            )
        elif bar.close < or_low and bar.delta < 0:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-short",
            )

        if order_id is not None:
            self._traded_this_session = True
            self._entry_id = order_id
            self._stop_dist = stop_dist
            self._breakeven_armed = False

    def _manage_open_position(self, ctx, bar) -> None:
        """Move the bracket stop to breakeven after a partly favourable move."""
        if self._breakeven_armed or self._entry_id is None:
            return

        legs = ctx.find_orders(parent_id=self._entry_id)
        for leg in legs:
            if leg.order_type != OrderType.STOP:
                continue

            entry = ctx.average_entry_price
            trigger_dist = self._stop_dist * self.breakeven_trigger_mult
            if trigger_dist <= 0:
                return

            if ctx.position > 0 and bar.close >= entry + trigger_dist:
                # Long trade: bring the protective sell stop up to breakeven.
                ctx.modify(leg.id, trigger=entry)
                self._breakeven_armed = True
            elif ctx.position < 0 and bar.close <= entry - trigger_dist:
                # Short trade: bring the protective buy stop down to breakeven.
                ctx.modify(leg.id, trigger=entry)
                self._breakeven_armed = True
            return

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
