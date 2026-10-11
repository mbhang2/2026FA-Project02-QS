"""Opening-range breakout strategy.

The campaign question is whether a move out of the regular-hours opening range
holds or the range wins.  The first four trials all took the *range wins* side
and faded the first close outside the range; all were negative.  This strategy
takes the opposite side: the move holds.

Opening range:
    The high and low of the first 30 minutes of regular-hours trading
    (`08:30 CT` to `09:00 CT`), measured on 5-minute bars.  The regular-hours
    open is identified as 15.5 hours after the start of the CME Globex session
    (`17:00 CT`), so with bars anchored to the session open this is a fixed bar
    offset and uses no lookahead.

Signal:
    After the range is known, if a completed 5-minute bar closes above the
    opening range high, the strategy buys the breakout.  If it closes below the
    opening range low, it sells.  One trade per session, so the rule acts on
    the ordinary run of sessions rather than a handful.

Exit:
    The engine's bracket arms immediately on the entry fill.  The stop is half
    the opening-range height, while the target is one and a half range heights,
    giving the trade room to run if the breakout continues.  Any open position
    is flattened at `15:00 CT` (or earlier on a short session), because the
    engine does not flatten positions automatically.
"""

from __future__ import annotations

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first regular-hours opening-range breakout in the breakout direction."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Flatten by 15:00 CT, giving a full hour before the 16:00 CT session close.
    RTH_EXIT_DELAY_SEC = 22 * 3600

    # Do not open a new trade in the last 30 minutes before the scheduled exit.
    ENTRY_CUTOFF_DELAY_SEC = RTH_EXIT_DELAY_SEC - 30 * 60

    # If a session ends early (half-day), force the exit this long before end.
    SESSION_EXIT_MARGIN_SEC = 10 * 60

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        min_range: float = 2.0,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range

        self._session = None
        self._range_high = None
        self._range_low = None
        self._range_count = 0
        self._traded_this_session = False
        self._exit_ordered = False

    def setup(self, ctx) -> None:
        # emit_empty=True guarantees a callback every 5m even if no trade
        # occurs, which matters for computing the opening range and for
        # flattening on thin session closes.
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # New session: reset all session-local state.
        if self._session != ctx.session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._range_count = 0
            self._traded_this_session = False
            self._exit_ordered = False

        sec_open = (bar.open_ts - ctx.session_start) // NS_PER_SECOND

        # Build the opening range from the first 30 minutes of RTH.
        # Do this during warmup too, so the range is available on the first
        # scored session of each run.
        if self.RTH_OPEN_DELAY_SEC <= sec_open < self.RTH_OPEN_DELAY_SEC + self.OPENING_RANGE_SEC:
            if bar.tick_count > 0:
                high = bar.high
                low = bar.low
                self._range_high = high if self._range_high is None else max(self._range_high, high)
                self._range_low = low if self._range_low is None else min(self._range_low, low)
                self._range_count += 1
            return

        if not ctx.actionable:
            return

        if self._range_high is None or self._range_low is None:
            return

        # If we are in a position, manage the session-time exit.  The engine's
        # bracket handles the stop/target legs; this handles not holding
        # overnight.
        if ctx.position:
            if self._exit_ordered:
                return
            exit_time = min(
                ctx.session_start + self.RTH_EXIT_DELAY_SEC * NS_PER_SECOND,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            if bar.close_ts >= exit_time:
                ctx.cancel_all()
                ctx.close()
                self._exit_ordered = True
            return

        if self._traded_this_session or ctx.working_order_ids:
            return

        # Do not enter during the last window before the scheduled exit.
        entry_cutoff = ctx.session_start + self.ENTRY_CUTOFF_DELAY_SEC * NS_PER_SECOND
        if bar.close_ts >= entry_cutoff:
            return

        # Never act on an empty bar; the close there is just a carried price.
        if bar.tick_count == 0:
            return

        rng = self._range_high - self._range_low
        if rng < self.min_range:
            return

        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        # Take the breakout in the direction of the first close outside the range.
        if bar.close > self._range_high:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-breakout-long",
            )
        elif bar.close < self._range_low:
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
