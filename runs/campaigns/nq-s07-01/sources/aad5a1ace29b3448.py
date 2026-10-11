"""Opening-range breakout with order-flow and volume confirmation.

The campaign asks whether a move out of the regular-hours opening range holds
or the range wins.  The best prior strategy (#11) took the *move holds* side,
traded the first 5-minute close outside the 30-minute opening range whose
`bar.delta` sign agreed with the break direction, and protected the trade with a
symmetric half-range bracket.  It scored +0.0438.

This proposal keeps exactly that structure but adds one mild participation
filter: before 10:00 CT, the breakout bar's total volume must be at least
`volume_mult` times the average volume of the bars that defined the opening
range.  A low-volume break is more likely to be noise or a stop-run that gets
absorbed; requiring normal market participation makes the "move holds" signal
cleaner.  After 10:00 CT the filter is relaxed back to the #11 sign-only rule,
so the strategy still acts on the ordinary run of sessions rather than only on
a handful.  Brackets remain symmetric at half the opening-range height and any
open position is flattened before the close.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first volume-confirmed opening-range breakout."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Until this time (10:00 CT) the breakout bar's volume must exceed the
    # average volume of the opening-range bars by `volume_mult`.  After this
    # deadline the condition is dropped to keep exposure on ordinary sessions.
    VOLUME_CONFIRM_DEADLINE_SEC = RTH_OPEN_DELAY_SEC + OPENING_RANGE_SEC + 3600

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
        volume_mult: float = 0.75,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.volume_mult = volume_mult

        self._session: int = -1
        self._rth_open_ts: int = 0
        self._or_end_ts: int = 0
        self._volume_confirm_deadline_ts: int = 0
        self._entry_cutoff_ts: int = 0
        self._exit_ts: int = 0
        self._or_high: Optional[float] = None
        self._or_low: Optional[float] = None
        self._or_volume_total: int = 0
        self._or_volume_bars: int = 0
        self._or_avg_volume: float = 0.0
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
            self._volume_confirm_deadline_ts = (
                ctx.session_start + self.VOLUME_CONFIRM_DEADLINE_SEC * NS_PER_SECOND
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
            self._or_volume_total = 0
            self._or_volume_bars = 0
            self._or_avg_volume = 0.0
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
                    self._or_volume_total += int(bar.volume)
                    self._or_volume_bars += 1
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
                    self._or_avg_volume = (
                        self._or_volume_total / self._or_volume_bars
                        if self._or_volume_bars > 0
                        else 0.0
                    )
                # Never act on a bar that is part of the range definition.
                return

            # First bar whose open is at/after the range end: range is complete.
            self._range_complete = True
            self._or_avg_volume = (
                self._or_volume_total / self._or_volume_bars
                if self._or_volume_bars > 0
                else 0.0
            )

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

        # Same direction filter as #11: the breakout bar's signed order flow
        # must agree with the direction of the break.
        long_signal = bar.close > or_high and bar.delta > 0
        short_signal = bar.close < or_low and bar.delta < 0

        if not (long_signal or short_signal):
            return

        # Volume participation filter.  Before 10:00 CT a breakout must occur
        # on at least normal volume relative to the opening range itself;
        # afterwards only the delta sign is required so that the strategy keeps
        # acting on the ordinary run of sessions.
        if bar.close_ts < self._volume_confirm_deadline_ts:
            if self._or_avg_volume <= 0.0:
                return
            if float(bar.volume) < self.volume_mult * self._or_avg_volume:
                return

        order_id = None
        if long_signal:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-volume-long",
            )
        elif short_signal:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-volume-short",
            )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
