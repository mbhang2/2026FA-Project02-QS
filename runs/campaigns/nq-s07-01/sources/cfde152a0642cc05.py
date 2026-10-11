"""Opening-range breakout with stronger order-flow confirmation (hold side only).

The campaign asks whether a move out of the regular-hours opening range holds
or the range wins.  Prior trial #11 took the *move holds* side: it bought an
upward break when the breakout bar's signed order flow agreed with the
direction, and sold a downward break when delta agreed.  It simply required
`bar.delta` to have the correct sign.

This strategy is a refinement of #11.  It still only trades the move-holds
side, but it now requires the breakout bar's signed order flow to be
*decisively* with the move, measured as `delta / volume`.  A bar that closes
outside the range with only a tiny positive delta (for an up break) is treated
as weak and skipped, in favour of a later breakout bar whose order flow is
more decisive.  This keeps the strategy active on the ordinary run of sessions
while avoiding breakouts that happen on absorbable, low-conviction flow.

Opening range:
    30 minutes of regular-hours trading (08:30 CT to 09:00 CT), measured on
    5-minute bars.  With bars anchored to the CME Globex session open
    (17:00 CT), this is a fixed bar offset and involves no lookahead.

Exit:
    Entry is protected immediately by the engine's bracket, with stop and
    target both equal to half the opening-range height.  Any open position is
    flattened by 15:00 CT or earlier on short sessions.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first decisive order-flow-confirmed opening-range breakout."""

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
        stop_mult: float = 0.5,
        target_mult: float = 0.5,
        min_range: float = 2.0,
        delta_frac: float = 0.10,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.delta_frac = delta_frac

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
            self._entry_cutoff_ts = (
                self._exit_ts - self.ENTRY_CUTOFF_BEFORE_EXIT_SEC * NS_PER_SECOND
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
                    if self._or_high is None or high > self._or_high:
                        self._or_high = high
                    if self._or_low is None or low < self._or_low:
                        self._or_low = low

                if bar.close_ts >= self._or_end_ts:
                    self._range_complete = True
                # Never act on a bar that is part of the range definition.
                return

            # First bar whose open is at/after the range end: range complete.
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
        if bar.tick_count == 0 or bar.volume <= 0:
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

        # Signed order flow as a fraction of total bar volume.  Weak flow is
        # treated as uninformative and the bar is skipped.
        flow_ratio = bar.delta / bar.volume

        order_id = None

        if bar.close > or_high and flow_ratio >= self.delta_frac:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-decisive-long",
            )
        elif bar.close < or_low and flow_ratio <= -self.delta_frac:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-decisive-short",
            )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
