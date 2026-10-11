"""Opening-range breakout with order-flow *magnitude* confirmation.

Campaign question: when price leaves the regular-hours opening range, does the
move hold or does the range win?

Prior trial #11 took the move-holds side and traded the first 5-minute close
outside the 30-minute opening range whose `bar.delta` sign agreed with the
break direction.  It scored +0.0438, the best so far.  This strategy keeps that
structure but strengthens the order-flow condition: a breakout bar must not only
have the right delta sign, it must have a meaningful signed-flow imbalance
relative to the bar's total volume.  A bar that closes outside the range but on
nearly balanced buy/sell flow is two-sided and is more likely to be absorbed;
waiting for the first breakout bar whose delta magnitude is at least
`delta_ratio_min` of volume trades the same story with less noise.

If a high-quality breakout does not appear in the first part of the session the
strategy relaxes back to the #11 condition (delta sign only) so it continues to
act on the ordinary run of sessions rather than only on a handful.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first order-flow-confirmed opening-range breakout."""

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

    # Require the breakout bar's signed-flow imbalance to be at least this
    # fraction of the bar's total volume before this deadline.  After this the
    # strategy falls back to the sign-only rule from #11.
    DELTA_CONFIRM_DEADLINE_SEC = 17 * 3600  # 10:00 CT

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 0.5,
        min_range: float = 2.0,
        delta_ratio_min: float = 0.10,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.delta_ratio_min = delta_ratio_min

        self._session: int = -1
        self._rth_open_ts: int = 0
        self._or_end_ts: int = 0
        self._entry_cutoff_ts: int = 0
        self._exit_ts: int = 0
        self._delta_confirm_deadline_ts: int = 0
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
            self._delta_confirm_deadline_ts = (
                ctx.session_start + self.DELTA_CONFIRM_DEADLINE_SEC * NS_PER_SECOND
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

        # The first close outside the range whose signed order flow confirms
        # the direction of the break.  A delta in the opposite direction
        # suggests the move is being absorbed and is more likely to fail.
        long_signal = bar.close > or_high and bar.delta > 0
        short_signal = bar.close < or_low and bar.delta < 0

        if not (long_signal or short_signal):
            return

        # Strength gate: early in the session, require the signed-flow
        # imbalance to represent a meaningful fraction of the bar's total
        # volume.  After the deadline, fall back to the sign-only condition so
        # the strategy keeps acting on the ordinary run of sessions.
        volume = int(bar.volume)
        if volume <= 0:
            delta_ratio = 0.0
        else:
            delta_ratio = abs(bar.delta) / volume

        threshold = self.delta_ratio_min
        if bar.close_ts >= self._delta_confirm_deadline_ts:
            threshold = 0.0

        if delta_ratio < threshold:
            return

        order_id = None
        if long_signal:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-strong-long",
            )
        elif short_signal:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-strong-short",
            )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
