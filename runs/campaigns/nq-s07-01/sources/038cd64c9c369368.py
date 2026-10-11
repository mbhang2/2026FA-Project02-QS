"""Opening-range breakout with strong close and early failure exit.

The best prior strategy (#36) traded the first delta-confirmed 5-minute close
beyond a 30-minute RTH opening range, buffered by 10% of range height, with a
symmetric half-range bracket and a strong-close requirement.  It scored +0.1587
with 362 trades over 384 sessions.

This proposal keeps #36's opening-range construction, 10% buffer, delta
confirmation, close-strength filter, half-range bracket, 15:00 CT flatten and
14:30 CT entry cutoff exactly.  It adds one risk-management rule: after entry,
if a subsequent 5-minute bar closes back inside the opening range (below the
range high for a long, or above the range low for a short), the position is
flattened immediately at market instead of waiting for the half-range stop.

The move-holds account says a genuine breakout should remain outside the range.
If price returns inside the range, the breakout has failed; the half-range stop
then gives back far more than necessary.  Exiting at the range boundary turns a
likely -0.5r stop into roughly a -0.1r loss.  This keeps the strategy active on
the ordinary run of sessions while cutting the cost of the "range wins" outcome
the campaign is asking about.

Opening range:
    High and low of the first 30 minutes of regular-hours trading
    (`08:30 CT` to `09:00 CT`), measured on 5-minute bars.  The RTH open is
    15.5 hours after the CME Globex session open (`17:00 CT`); with bars
    anchored to the session open this is a fixed bar offset and involves no
    lookahead.

Signal:
    After the range is known, the first completed 5-minute bar that closes
    above the range high by at least `breakout_buffer_mult` times the range
    height with `bar.delta > 0` and with its close in the top
    `close_strength` fraction of the bar's own range is bought.  The first
    completed bar that closes below the range low by at least the same buffer
    with `bar.delta < 0` and its close in the bottom `close_strength` fraction
    of its range is sold.  One trade per session.

Exit:
    Entry is protected immediately by the engine's bracket, with stop and
    target offsets both equal to half the opening-range height.  In addition,
    a close back inside the opening range triggers an immediate market flatten,
    cancelling the bracket.  Any open position is also flattened by `15:00 CT`
    or earlier on short sessions, because the engine does not flatten positions
    automatically.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first delta-confirmed opening-range breakout whose bar closes
    strongly through the level, and exit early if price closes back inside the
    range."""

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
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.breakout_buffer_mult = breakout_buffer_mult
        self.close_strength = close_strength

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

        # Manage an open position: time exit first, then early failure exit if
        # price closes back inside the opening range.
        if ctx.position:
            if self._exit_ordered:
                return

            # Mandatory flatten before the session ends.
            if bar.close_ts >= self._exit_ts:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True
                return

            # Early failure exit: a long breakout fails if a completed bar
            # closes below the opening-range high; a short fails if it closes
            # above the opening-range low.
            if (
                self._or_high is not None
                and ctx.position > 0
                and bar.close < self._or_high
            ):
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True
                return

            if (
                self._or_low is not None
                and ctx.position < 0
                and bar.close > self._or_low
            ):
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True
                return

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

        # The bar must close strongly through the level, not merely print it.
        body = bar.high - bar.low
        if body <= 0:
            return
        close_location = (bar.close - bar.low) / body

        buffer = rng * self.breakout_buffer_mult
        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        order_id = None

        # Trade the first close outside the range by at least the buffer whose
        # signed order flow confirms the direction and whose close is in the
        # strong part of its bar.
        if (
            bar.close > or_high + buffer
            and bar.delta > 0
            and close_location >= self.close_strength
        ):
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-strong-close-earlyfail-long",
            )
        elif (
            bar.close < or_low - buffer
            and bar.delta < 0
            and close_location <= (1.0 - self.close_strength)
        ):
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-strong-close-earlyfail-short",
            )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
