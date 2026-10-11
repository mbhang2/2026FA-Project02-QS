"""Opening-range breakout on 1-minute confirmation bars.

The campaign asks whether a move out of the regular-hours opening range holds or
the range wins.  The best prior strategy (#11) took the *move holds* side:
after the 30-minute RTH opening range, it bought the first 5-minute close above
the range high with positive signed order flow (`bar.delta`) and sold the mirror
image, protecting the trade with a symmetric half-range bracket.  It scored
+0.0438.

This proposal keeps the same thesis but moves the entry signal from 5-minute
bars to 1-minute bars.  Waiting for a 5-minute close means acting as late as
four minutes after the first tradable 1-minute close outside the range.  If the
breakout's first order-flow-confirmed minute close above/below the range occurs
at 09:01, we enter then rather than at 09:05.  Entering closer to the range
boundary should improve the fill and leave more of the continuation move for
the bracket to capture.

Opening range:
    High and low of the first 30 minutes of regular-hours trading
    (`08:30 CT` to `09:00 CT`), measured on 5-minute bars.  The regular-hours
    open is 15.5 hours after the CME Globex session open (`17:00 CT`); with bars
    anchored to the session open this is a fixed bar offset and involves no
    lookahead.

Signal:
    After the range is known, the first completed 1-minute bar that closes
    above the range high with `bar.delta > 0` is bought.  The first completed
    1-minute bar that closes below the range low with `bar.delta < 0` is sold.
    One trade per session.

Exit:
    Entry is protected immediately by the engine's bracket, with stop and
    target offsets both equal to half the opening-range height.  Any open
    position is flattened by `15:00 CT` or earlier on short sessions, because
    the engine does not flatten positions automatically.  Flattening is checked
    on 1-minute bars for a more precise exit time than a 5-minute bar gives.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first delta-confirmed opening-range breakout on 1-minute bars."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Flatten by 15:00 CT, giving a full hour before the 16:00 CT session close.
    EXIT_DELAY_SEC = 22 * 3600

    # If a session ends early (half-day), force the exit this long before end.
    SESSION_EXIT_MARGIN_SEC = 10 * 60

    # No new entry in the last 30 minutes before the effective exit.
    ENTRY_CUTOFF_BEFORE_EXIT_SEC = 30 * 60

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 0.5,
        min_range: float = 2.0,
    ):
        self.interval_5m = "5m"
        self.interval_1m = "1m"
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
        # Guarantee callbacks every 1 and 5 minutes even when no trade occurs,
        # so the range is measured on time and positions can be flattened near
        # the session close even on thin sessions.
        ctx.subscribe(self.interval_5m, emit_empty=True)
        ctx.subscribe(self.interval_1m, emit_empty=True)

    def on_bars(self, ctx) -> None:
        bar5 = ctx.bars.get(self.interval_5m)
        bar1 = ctx.bars.get(self.interval_1m)

        if bar5 is None and bar1 is None:
            return

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

        # Build the opening range from 5-minute bars only.  The 1-minute bars
        # are not used for the range definition.
        if bar5 is not None and not self._range_complete:
            if bar5.close_ts <= self._rth_open_ts:
                pass
            elif bar5.open_ts < self._or_end_ts:
                if bar5.tick_count > 0:
                    high = bar5.high
                    low = bar5.low
                    if self._or_high is None:
                        self._or_high = high
                    elif high > self._or_high:
                        self._or_high = high
                    if self._or_low is None:
                        self._or_low = low
                    elif low < self._or_low:
                        self._or_low = low

                if bar5.close_ts >= self._or_end_ts:
                    self._range_complete = True
            else:
                # First 5-minute bar whose open is at/after the range end:
                # the range is complete.
                self._range_complete = True

        # Signal, flattening, and late-entry logic runs on 1-minute bars.
        if bar1 is None:
            return

        if not self._range_complete:
            return

        if not ctx.actionable:
            return

        # Mandatory flatten before the session ends.  The engine's bracket
        # handles stop/target; this prevents an overnight hold.
        if ctx.position:
            if bar1.close_ts >= self._exit_ts and not self._exit_ordered:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True
            return

        if self._traded_this_session:
            return
        if bar1.close_ts <= self._or_end_ts:
            # The 1-minute bar that closes exactly at the range end is part of
            # the range definition window and must not be traded.
            return
        if bar1.close_ts >= self._entry_cutoff_ts:
            return
        if bar1.tick_count == 0:
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

        order_id = None

        # Trade the first 1-minute close outside the range whose signed order
        # flow confirms the direction of the break.
        if bar1.close > or_high and bar1.delta > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-long",
            )
        elif bar1.close < or_low and bar1.delta < 0:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-short",
            )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
