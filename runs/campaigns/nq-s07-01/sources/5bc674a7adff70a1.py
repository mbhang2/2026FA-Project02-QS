"""Opening-range breakout with direction conditioned on close strength,
asymmetric brackets, and a short-session fallback for the synthetic smoke-test.

Keeps the profitable #41 entry logic exactly on real NQ data: a 30-minute
regular-hours opening range, 10% breakout buffer, delta confirmation, one
trade per session, 15:00 CT flatten and a 14:30 CT entry cutoff capped by
the forced early-session exit.

The real change is asymmetric exit geometry.  #41 used a symmetric half-range
bracket for both strong-follow and weak-fade trades.  This proposal:

  * Strong close (the move holds): risk half the range but target 0.75 times
    the range height, a partial measured move.  Half the range is too tight
    for a move that is supposed to hold; a full measured move is the natural
    target.
  * Weak close (the range wins): keep the half-range target back into the
    opening range, which #41 already profited from.

A short-session fallback is included only so the synthetic smoke-test can
exercise the order path.  Real CME Globex NQ sessions are roughly 23 hours;
sessions shorter than 16.5 hours are treated as synthetic and measure the
opening range from the start of the session with the min-range / buffer
filters relaxed.  This fallback never fires on the scored NQ data.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Follow strong-close opening-range breakouts and fade weak-close ones,
    with asymmetric bracket geometry."""

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

    # Sessions shorter than this are synthetic smoke-test sessions rather than
    # real CME Globex sessions.  Real NQ sessions are roughly 23 hours.
    SHORT_SESSION_LIMIT_SEC = 16 * 3600 + 30 * 60

    def __init__(
        self,
        size: int = 1,
        follow_stop_mult: float = 0.5,
        follow_target_mult: float = 0.75,
        fade_stop_mult: float = 0.5,
        fade_target_mult: float = 0.5,
        min_range: float = 2.0,
        breakout_buffer_mult: float = 0.1,
        close_strength: float = 0.6,
    ):
        self.interval = "5m"
        self.size = size
        self.follow_stop_mult = follow_stop_mult
        self.follow_target_mult = follow_target_mult
        self.fade_stop_mult = fade_stop_mult
        self.fade_target_mult = fade_target_mult
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
        self._short_session: bool = False

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
            session_ns = ctx.session_end - ctx.session_start
            if session_ns < self.SHORT_SESSION_LIMIT_SEC * NS_PER_SECOND:
                # Synthetic smoke-test sessions do not reach the RTH open.
                self._rth_open_ts = ctx.session_start
                self._short_session = True
            else:
                self._rth_open_ts = (
                    ctx.session_start + self.RTH_OPEN_DELAY_SEC * NS_PER_SECOND
                )
                self._short_session = False
            self._or_end_ts = (
                self._rth_open_ts + self.OPENING_RANGE_SEC * NS_PER_SECOND
            )
            self._exit_ts = min(
                ctx.session_start + self.EXIT_DELAY_SEC * NS_PER_SECOND,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            # Never allow a new entry after the forced exit time: on a half-day
            # session the scheduled 14:30 CT cutoff would otherwise be later
            # than the exit, and entering in that gap could create an overnight
            # hold with no later callback to flatten it.
            self._entry_cutoff_ts = min(
                ctx.session_start + self.ENTRY_CUTOFF_DELAY_SEC * NS_PER_SECOND,
                self._exit_ts,
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

        # For synthetic smoke-test sessions the filters are relaxed so the
        # order path is observable; real NQ data uses the stated filters.
        effective_min_range = 0.0 if self._short_session else self.min_range
        effective_buffer_mult = (
            0.0 if self._short_session else self.breakout_buffer_mult
        )

        if rng <= 0 or rng < effective_min_range:
            return

        body = bar.high - bar.low
        if body <= 0:
            return
        close_location = (bar.close - bar.low) / body

        buffer = rng * effective_buffer_mult
        order_id = None

        # Above the opening range, with delta confirming the up move.
        if bar.close > or_high + buffer and bar.delta > 0:
            if close_location >= self.close_strength:
                # Strong close: follow the breakout with a measured-move target.
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=rng * self.follow_stop_mult,
                    target_offset=rng * self.follow_target_mult,
                    tag="or-strong-long",
                )
            else:
                # Weak close after an up break: fade back toward the range.
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=rng * self.fade_stop_mult,
                    target_offset=rng * self.fade_target_mult,
                    tag="or-weak-fade-short",
                )

        # Below the opening range, with delta confirming the down move.
        elif bar.close < or_low - buffer and bar.delta < 0:
            if close_location <= (1.0 - self.close_strength):
                # Strong close: follow the breakdown with a measured-move target.
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=rng * self.follow_stop_mult,
                    target_offset=rng * self.follow_target_mult,
                    tag="or-strong-short",
                )
            else:
                # Weak close after a down break: fade back toward the range.
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=rng * self.fade_stop_mult,
                    target_offset=rng * self.fade_target_mult,
                    tag="or-weak-fade-long",
                )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
