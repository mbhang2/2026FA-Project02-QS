"""Opening-range breakout: act only on the first clean close beyond the range.

The best prior strategy (#23) traded the first delta-confirmed 5-minute close
beyond a 30-minute RTH opening range, buffered by 10% of the range height, with
a symmetric half-range bracket.  It scored +0.1290, but its rule has a subtle
chase: if the first close beyond the range has the wrong signed order flow, the
strategy simply waits for a later bar that closes beyond the range with the
right delta.  That later entry is no longer the first breakout -- it is entering
after the range has already been left and price has run further.

This proposal keeps #23's range construction, 10% buffer, delta confirmation,
and half-range bracket exactly, but changes the state machine: the first bar to
close beyond the buffered range is decisive.  If its delta confirms the
direction, the trade is taken.  If its delta does not confirm, the session is
over for this strategy.  The claim is that the first clean close outside the
range is the event the question is about; if order flow disagrees with that
first event, chasing a later close is a worse entry with less room before the
target and a wider adverse excursion before the stop.

Opening range:
    High and low of the first 30 minutes of regular-hours trading
    (`08:30 CT` to `09:00 CT`), measured on 5-minute bars.  The RTH open is
    15.5 hours after the CME Globex session open (`17:00 CT`); with bars
    anchored to the session open this is a fixed bar offset and involves no
    lookahead.

Signal:
    After the range is known, watch for the first completed 5-minute bar whose
    close is above the range high by at least `breakout_buffer_mult` times the
    range height or below the range low by the same buffer.  If that first
    close is above with `bar.delta > 0`, buy.  If it is below with
    `bar.delta < 0`, sell.  If the first close beyond the range has the wrong
    delta, do not trade again this session.  One trade per session at most.

Exit:
    Entry is protected immediately by the engine's bracket, with stop and
    target offsets both equal to half the opening-range height.  Any open
    position is flattened by `15:00 CT` or earlier on short sessions, because
    the engine does not flatten positions automatically.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first clean close beyond the RTH opening range, or skip the
    session if that first close lacks order-flow confirmation."""

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
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.breakout_buffer_mult = breakout_buffer_mult

        self._session: int = -1
        self._rth_open_ts: int = 0
        self._or_end_ts: int = 0
        self._entry_cutoff_ts: int = 0
        self._exit_ts: int = 0
        self._or_high: Optional[float] = None
        self._or_low: Optional[float] = None
        self._range_complete: bool = False
        self._breakout_seen: bool = False
        self._traded_this_session: bool = False
        self._exit_ordered: bool = False

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
            self._breakout_seen = False
            self._traded_this_session = False
            self._exit_ordered = False

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

        if self._traded_this_session or self._breakout_seen:
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

        # The first close beyond the buffered range is decisive.  If it breaks
        # upward with positive delta, trade long.  If it breaks downward with
        # negative delta, trade short.  If it breaks without the confirming
        # delta, record that the breakout occurred and do not chase a later one.
        if bar.close > or_high + buffer:
            self._breakout_seen = True
            if bar.delta > 0:
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-first-clean-long",
                )
                if order_id is not None:
                    self._traded_this_session = True

        elif bar.close < or_low - buffer:
            self._breakout_seen = True
            if bar.delta < 0:
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-first-clean-short",
                )
                if order_id is not None:
                    self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
