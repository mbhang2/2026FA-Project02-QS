"""Opening-range breakout, volume-confirmed.

The campaign asks whether a move out of the regular-hours opening range holds
or the range wins.  In this campaign the *range wins* attempts were negative
and the first *move holds* breakout attempt (#6) was the only positive one,
with a symmetric stop/target of 0.5 times the opening-range height.

This proposal keeps that successful core exactly -- same 30-minute opening
range, same 5m decision bar, same one-trade-per-session rule, and the same
0.5/0.5 bracket -- and makes one change to the entry filter.  The question
asks us to distinguish a flow-driven range break from an informed move, and the
bar fields we already have at 5m resolution let us do that cheaply:

    `bar.delta` is signed order flow for the bar (`buy_volume - sell_volume`),
    `bar.volume` is total volume.

Hypothesis: a close outside the opening range is more likely to hold when the
bar that breaks the range is itself accompanied by directional order flow and
normal-to-high participation.  We therefore require that the breakout bar:

1. have volume at least 0.75x the average 5m volume observed inside the opening
   range, and
2. have `bar.delta` in the direction of the break (positive for longs,
   negative for shorts).

These are two facets of the same idea, not a multi-stage confirmation ladder:
a breakout bar that closes outside on light volume or with delta against the
break is treated as noise and skipped.  If no qualifying break occurs, the
session is not traded.

Everything else is the tested #6 mechanics: the opening range is the first 30
minutes of regular-hours trading (08:30 CT to 09:00 CT), measured from the CME
Globex session open (17:00 CT).  Entry is the first qualifying 5m close outside
the range.  The bracket is armed by the engine at fill, with stop and target
each at half the opening-range height.  Positions are force-flattened at
15:00 CT, or 10 minutes before an early session close.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first volume-confirmed break of the RTH opening range."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Flatten by 15:00 CT, one hour before the 16:00 CT session close.
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
    ) -> None:
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.volume_mult = volume_mult

        self._session: int = -1
        self._rth_open_ts: int = 0
        self._or_end_ts: int = 0
        self._entry_cutoff_ts: int = 0
        self._exit_ts: int = 0
        self._or_high: Optional[float] = None
        self._or_low: Optional[float] = None
        self._or_volumes: list = []
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
            self._entry_cutoff_ts = ctx.session_start + self.ENTRY_CUTOFF_DELAY_SEC * NS_PER_SECOND
            self._exit_ts = min(
                ctx.session_start + self.EXIT_DELAY_SEC * NS_PER_SECOND,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            self._or_high = None
            self._or_low = None
            self._or_volumes = []
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
                    self._or_volumes.append(bar.volume)

                if bar.close_ts >= self._or_end_ts:
                    self._range_complete = True
                # Never act on a bar that is part of the range definition.
                return

            # First bar whose open is at/after the range end: range is complete.
            self._range_complete = True

        if not ctx.actionable:
            return

        # Mandatory flatten before the session ends.  The engine's bracket
        # handles the stop/target legs; this prevents an overnight hold.
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

        # Volume confirmation: the break must be on normal-to-heavy volume
        # relative to what the opening range itself saw.
        if not self._or_volumes:
            return
        or_avg_volume = sum(self._or_volumes) / len(self._or_volumes)
        if bar.volume < self.volume_mult * or_avg_volume:
            return

        # Directional order-flow confirmation: the break bar's delta must agree
        # with the breakout direction.  This excludes closes that drift outside
        # the range against the aggressive order flow.
        signal = None
        if bar.close > or_high:
            if bar.delta > 0:
                signal = "long"
        elif bar.close < or_low:
            if bar.delta < 0:
                signal = "short"
        if signal is None:
            return

        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        if signal == "long":
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-breakout-long-volume",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-breakout-short-volume",
            )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
