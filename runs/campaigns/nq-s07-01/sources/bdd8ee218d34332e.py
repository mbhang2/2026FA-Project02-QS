"""Opening-range breakout with a margin filter and a time fallback.

The campaign asks whether a move out of the regular-hours opening range holds
or the range wins.  The best prior strategy (#11) took the *move holds* side
with a 30-minute opening range, entered on the first 5-minute close outside the
range whose signed order flow (`bar.delta`) confirmed the direction, and used a
symmetric bracket at half the range height.  It scored +0.0438.

This strategy keeps that structure but adds one idea: an early breakout that
only barely clears the range is more likely to be noise and fail, while a
breakout that clears the range by a meaningful margin is the move the question
is about.

Specifically, before 10:30 CT a breakout is tradeable only if it clears the
opening range by at least `break_margin_mult` of the range height, in addition
to having confirming delta.  If the market has not produced such a strong
confirmed breakout by 10:30 CT, the rule relaxes to the #11 rule -- any
delta-confirmed close outside the range -- so the strategy still acts on the
ordinary run of sessions rather than only on a handful.

Opening range:
    High and low of the first 30 minutes of regular-hours trading
    (`08:30 CT` to `09:00 CT`), measured on 5-minute bars.  The regular-hours
    open is 15.5 hours after the CME Globex session open (`17:00 CT`); with
    bars anchored to the session open this is a fixed bar offset and involves
    no lookahead.

Signal:
    After the range is known, the first completed 5-minute bar that closes
    outside the range with `bar.delta` agreeing with the direction is entered.
    Before the fallback time the close must also exceed the range by
    `break_margin_mult * range_height`; after the fallback time any
    delta-confirmed close outside the range is accepted.  One trade per
    session.

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
    """Trade the first sufficiently strong delta-confirmed opening-range breakout."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Flatten by 15:00 CT, giving a full hour before the 16:00 CT session close.
    EXIT_DELAY_SEC = 22 * 3600

    # Do not open a new trade in the last 30 minutes before the scheduled exit.
    ENTRY_CUTOFF_DELAY_SEC = EXIT_DELAY_SEC - 30 * 60

    # If no strong breakout has appeared this long after the RTH open, relax
    # the margin requirement and accept any delta-confirmed close outside the
    # range.  08:30 CT + 2h = 10:30 CT.
    FALLBACK_AFTER_RTH_OPEN_SEC = 2 * 3600

    # If a session ends early (half-day), force the exit this long before end.
    SESSION_EXIT_MARGIN_SEC = 10 * 60

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 0.5,
        break_margin_mult: float = 0.15,
        min_range: float = 2.0,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.break_margin_mult = break_margin_mult
        self.min_range = min_range

        self._session: int = -1
        self._rth_open_ts: int = 0
        self._or_end_ts: int = 0
        self._fallback_ts: int = 0
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
            self._or_end_ts = self._rth_open_ts + self.OPENING_RANGE_SEC * NS_PER_SECOND
            self._fallback_ts = (
                self._rth_open_ts + self.FALLBACK_AFTER_RTH_OPEN_SEC * NS_PER_SECOND
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

        # Mandatory flatten before the session ends.  The engine's bracket
        # handles stop/target; this prevents an overnight hold.
        if ctx.position:
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

        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        # Before the fallback time, require the close to clear the range by a
        # meaningful margin.  This filters out the weakest early false breaks.
        # After the fallback time, any delta-confirmed close outside the range
        # is accepted so the strategy still participates on ordinary sessions.
        if bar.close_ts < self._fallback_ts:
            margin = rng * self.break_margin_mult
            strong_long = bar.close > or_high + margin and bar.delta > 0
            strong_short = bar.close < or_low - margin and bar.delta < 0
        else:
            strong_long = bar.close > or_high and bar.delta > 0
            strong_short = bar.close < or_low and bar.delta < 0

        order_id = None

        if strong_long:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-strong-long",
            )
        elif strong_short:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-strong-short",
            )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
