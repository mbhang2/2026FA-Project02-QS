"""Opening-range breakout with delta confirmation, asymmetric profit target,
and a near-session-close mandatory exit.

This is a direct variation of the best prior strategy (#11, score +0.0438).
The entry signal is unchanged: after the 30-minute regular-hours opening range
is known, the first completed 5-minute bar to close above the range high with
positive delta (`bar.delta > 0`) is bought, and the first close below the range
low with negative delta is sold.  One position per session.

Two changes are made, both on the thesis that a *confirmed* opening-range move
tends to hold:

1. The exit bracket is asymmetric.  The protective stop remains at half the
   opening-range height from the fill, but the profit target is widened to
   0.75 of the opening-range height.  This gives a 1.5:1 reward-to-risk ratio
   instead of the previous 1:1 half-range bracket, allowing a genuine move to
   pay more while risking the same distance.

2. Full-length sessions are no longer force-flattened at 15:00 CT.  The
   mandatory exit is moved to ten minutes before the scheduled session close
   (15:50 CT on a full Globex session), giving a still-open, bracket-managed
   winner extra time to reach its target.  Early-closing sessions continue to
   exit ten minutes before their own close.  No new entry is allowed later than
   14:30 CT, preserving the ordinary opening-range time window.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first delta-confirmed opening-range breakout with an asymmetric bracket."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # No new entry after 14:30 CT, regardless of the effective exit time.
    FIXED_ENTRY_CUTOFF_DELAY_SEC = 21 * 3600 + 30 * 60

    # When the effective exit is earlier than the fixed cutoff, do not enter in
    # the 30 minutes before that exit either.
    ENTRY_CUTOFF_BEFORE_EXIT_SEC = 30 * 60

    # Full-length sessions are exited at 15:50 CT, ten minutes before the
    # 16:00 CT close, rather than at 15:00 CT.
    EXIT_DELAY_SEC = 22 * 3600 + 50 * 60

    # If a session ends early, flatten this far before the scheduled end.
    SESSION_EXIT_MARGIN_SEC = 10 * 60

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 0.75,
        min_range: float = 2.0,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range

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

            scheduled_exit_ts = (
                ctx.session_start + self.EXIT_DELAY_SEC * NS_PER_SECOND
            )
            self._exit_ts = min(
                scheduled_exit_ts,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )

            fixed_cutoff = (
                ctx.session_start + self.FIXED_ENTRY_CUTOFF_DELAY_SEC * NS_PER_SECOND
            )
            self._entry_cutoff_ts = min(
                fixed_cutoff,
                self._exit_ts - self.ENTRY_CUTOFF_BEFORE_EXIT_SEC * NS_PER_SECOND,
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
        if rng <= self.min_range:
            return

        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        order_id = None

        # First close outside the range whose signed order flow confirms the
        # direction of the break.
        if bar.close > or_high and bar.delta > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-asym-delta-long",
            )
        elif bar.close < or_low and bar.delta < 0:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-asym-delta-short",
            )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
