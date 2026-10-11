"""Opening-range breakout with a modestly asymmetric bracket.

The campaign asks whether a move out of the regular-hours opening range holds
or the range wins.  The best prior trial (#6) took the *move holds* side with a
30-minute opening range, entered on the first 5-minute close outside the range,
and used a symmetric bracket at half the range height.  It was mildly positive.

The prior trial that widened the target to a full range height (#7) was clearly
negative, so we do not ask for a huge continuation.  This proposal keeps the
same opening-range construction and first-break entry, but changes two things
that reach the orders directly:

1. The bracket is now slightly asymmetric: stop at 0.40 * range and target at
   0.60 * range.  This raises the reward-to-risk ratio a little while staying
   far from the full-range target that failed.
2. `min_range` is lowered from 2.0 to 0.0.  On NQ an opening range below 2.0 is
   rare, but those sessions are still ordinary sessions and the campaign says a
   rule must act on the ordinary run of sessions, not only on favorable ones.

We also tie the entry cutoff to the effective exit time rather than to a fixed
clock.  The scheduled 15:00 CT flatten is still used on full sessions, but on an
early-closing session the no-new-trade window now ends 30 minutes before the
actual effective exit, preventing an entry that would be immediately flattened.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first break of the RTH opening range."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Flatten by 15:00 CT, one hour before the 16:00 CT session close.
    EXIT_DELAY_SEC = 22 * 3600

    # If a session ends early, force the exit this long before end.
    SESSION_EXIT_MARGIN_SEC = 10 * 60

    # No new entry in the last 30 minutes before the effective exit.
    ENTRY_CUTOFF_BEFORE_EXIT_SEC = 30 * 60

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.4,
        target_mult: float = 0.6,
        min_range: float = 0.0,
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
            self._rth_open_ts = ctx.session_start + self.RTH_OPEN_DELAY_SEC * NS_PER_SECOND
            self._or_end_ts = self._rth_open_ts + self.OPENING_RANGE_SEC * NS_PER_SECOND

            scheduled_exit_ts = ctx.session_start + self.EXIT_DELAY_SEC * NS_PER_SECOND
            self._exit_ts = min(
                scheduled_exit_ts,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            self._entry_cutoff_ts = self._exit_ts - self.ENTRY_CUTOFF_BEFORE_EXIT_SEC * NS_PER_SECOND

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
        # handles the stop/target; this prevents an overnight hold.
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

        if bar.close > or_high:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-breakout-long",
            )
        elif bar.close < or_low:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-breakout-short",
            )
        else:
            order_id = None

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
