"""Opening-range escape: follow strong confirmed breaks, fade all weak closes.

This keeps the best prior strategy (#41) exactly: a 30-minute regular-hours
opening range, 10% breakout buffer, 5-minute bars, one trade per session,
close-strength split at 0.6, half-range bracket, 15:00 CT flatten and
14:30 CT entry cutoff.  #41 followed strong-close delta-confirmed breakouts
and faded weak-close delta-confirmed breakouts.  It scored +0.1970.

The change here widens the fade side only.  #41 required the breakout bar's
signed order flow to confirm the escape before the weak close was faded.  A
weak close outside the opening range is itself evidence that the escape is
failing: price pushed out of the range but could not hold near the extreme.
When the close is weak, fading it does not need the delta to agree with the
breakout direction; if delta is positive on an up escape that is the original
failed-breakout case, and if delta is negative on an up escape the failure is
even clearer.  Follow trades still require delta confirmation, because a
strong-close follow is a statement that order flow and price agree.

Rule:
    After the range is known, look at the first completed 5-minute bar whose
    close is outside the range by at least `breakout_buffer_mult` times the
    range height.
    Above the range:
      * strong close (`close_location >= close_strength`) and `bar.delta > 0`
        -> buy the breakout;
      * weak close -> sell, regardless of delta.
    Below the range:
      * strong close (`close_location <= 1 - close_strength`) and
        `bar.delta < 0` -> sell the breakdown;
      * weak close -> buy, regardless of delta.
    One trade per session.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Follow strong confirmed opening-range escapes; fade all weak closes."""

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

        body = bar.high - bar.low
        if body <= 0:
            return
        close_location = (bar.close - bar.low) / body

        buffer = rng * self.breakout_buffer_mult
        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        order_id = None
        above = bar.close > or_high + buffer
        below = bar.close < or_low - buffer

        if above:
            strong = close_location >= self.close_strength
            if strong and bar.delta > 0:
                # Strong close with confirming order flow: the move holds.
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-strong-long",
                )
            elif not strong:
                # Weak close after an up escape: the range wins, no delta needed.
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-weak-fade-short",
                )
        elif below:
            strong = close_location <= (1.0 - self.close_strength)
            if strong and bar.delta < 0:
                # Strong close with confirming order flow: the move holds.
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-strong-short",
                )
            elif not strong:
                # Weak close after a down escape: the range wins, no delta needed.
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-weak-fade-long",
                )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
