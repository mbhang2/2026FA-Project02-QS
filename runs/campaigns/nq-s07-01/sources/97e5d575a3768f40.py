"""Opening-range breakout with asymmetric targets by signal type.

The best prior strategy (#41) follows strong-close opening-range breakouts and
fades weak-close breakouts, with a half-range stop and half-range target on
both sides.  It scored +0.1970.

This proposal keeps #41's opening-range construction, 10% buffer, delta
confirmation, close-strength rule, 15:00 CT flatten and 14:30 CT entry cutoff
exactly.  It changes only the target distance on follow trades: a strong-close
breakout that confirms "the move holds" is given a target equal to 0.75 times
the opening-range height, while the stop remains at 0.5 times the range.  The
"range wins" fade trades keep the proven half-range target.

Rationale: if a breakout is genuinely trend continuation, it should travel
further than half the opening range before stalling.  A half-range target on a
continuation leaves the second half of the move on the table.  The stop is not
widened, so the follow trades now have 1.5:1 reward-to-risk instead of 1:1.
The weak-close fade trades are unchanged because the prior experiment (#46)
showed that widening their target to a full range hurt performance.

Opening range:
    High and low of the first 30 minutes of regular-hours trading
    (`08:30 CT` to `09:00 CT`), measured on 5-minute bars.  The RTH open is
    15.5 hours after the CME Globex session open (`17:00 CT`); with bars
    anchored to the session open this is a fixed bar offset and involves no
    lookahead.

Signal:
    After the range is known, look at the first completed 5-minute bar whose
    close is outside the range by at least `breakout_buffer_mult` times the
    range height and whose signed order flow confirms the direction of the
    break (`bar.delta > 0` above the range, `bar.delta < 0` below it).

    For a bar above the range:
      * if its close is in the top `close_strength` fraction of its own body,
        buy the breakout (the move holds) with a 0.75-range target;
      * otherwise sell (the range wins) with a 0.5-range target.
    For a bar below the range:
      * if its close is in the bottom `close_strength` fraction of its body,
        sell the breakdown with a 0.75-range target;
      * otherwise buy with a 0.5-range target.

    One trade per session.

Exit:
    Entry is protected immediately by the engine's bracket.  The stop offset
    is always half the opening-range height.  Strong-close follow trades use a
    0.75-range target; weak-close fade trades use a 0.5-range target.  Any
    open position is flattened by `15:00 CT` or earlier on short sessions,
    because the engine does not flatten positions automatically.  The entry
    cutoff is additionally capped at the forced exit time on early-close
    sessions so a new position cannot be opened with no later callback to
    flatten it.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Follow strong-close opening-range breakouts, fade weak-close ones, with
    asymmetric targets favouring continuation trades."""

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
        follow_target_mult: float = 0.75,
        fade_target_mult: float = 0.5,
        min_range: float = 2.0,
        breakout_buffer_mult: float = 0.1,
        close_strength: float = 0.6,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.follow_target_mult = follow_target_mult
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
            self._exit_ts = min(
                ctx.session_start + self.EXIT_DELAY_SEC * NS_PER_SECOND,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            # Never allow a new trade after the forced exit time.  On an
            # early-close session the scheduled 14:30 CT cutoff is later than
            # the forced exit, and entering in that gap could leave a position
            # with no later callback to flatten it.
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
        if rng < self.min_range:
            return

        body = bar.high - bar.low
        if body <= 0:
            return
        close_location = (bar.close - bar.low) / body

        buffer = rng * self.breakout_buffer_mult
        stop_dist = rng * self.stop_mult
        follow_target_dist = rng * self.follow_target_mult
        fade_target_dist = rng * self.fade_target_mult

        order_id = None

        # Above the opening range, with delta confirming the up move.
        if bar.close > or_high + buffer and bar.delta > 0:
            if close_location >= self.close_strength:
                # Strong close: follow the breakout, larger target.
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=follow_target_dist,
                    tag="or-strong-long",
                )
            else:
                # Weak close after an up break: fade it, half-range target.
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=fade_target_dist,
                    tag="or-weak-fade-short",
                )

        # Below the opening range, with delta confirming the down move.
        elif bar.close < or_low - buffer and bar.delta < 0:
            if close_location <= (1.0 - self.close_strength):
                # Strong close: follow the breakdown, larger target.
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=follow_target_dist,
                    tag="or-strong-short",
                )
            else:
                # Weak close after a down break: fade it, half-range target.
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=fade_target_dist,
                    tag="or-weak-fade-long",
                )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
