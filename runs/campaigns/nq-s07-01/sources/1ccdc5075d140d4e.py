"""Opening-range fade with order-flow divergence filter.

The campaign asks whether a move out of the regular-hours opening range holds or
the range wins.  This strategy stays on the *range wins* side, but only fades a
breakout when the closed bar that left the range shows a signed-volume divergence
from its direction.  That is the closest the data lets us get to "the opening
flow was not price-sensitive": a bar closing above the range on non-positive
delta, or closing below the range on non-negative delta.

The regular-hours open is 15.5 hours after the CME Globex session open
(17:00 CT -> 08:30 CT).  The opening range is the first 30 minutes of RTH,
measured on 5-minute bars.  After the range is known, the strategy watches for
a 5-minute close outside it.  The divergence filter avoids fading every
breakout, which prior trials show is close to break-even before costs but loses
after commission.  One trade per session.

Exit is handled by the engine's bracket with stop and target both at half the
opening-range height, plus a mandatory flatten before the session close.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Fade opening-range breakouts only when order flow disagrees with price."""

    # Seconds from CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # Opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Do not open a new trade after 11:30 CT, keeping the test near the range.
    ENTRY_CUTOFF_DELAY_SEC = 18 * 3600 + 30 * 60

    # Flatten by 15:00 CT, giving a full hour before the 16:00 CT session close.
    EXIT_DELAY_SEC = 22 * 3600

    # If a session ends early, force the exit this long before the end.
    SESSION_EXIT_MARGIN_SEC = 10 * 60

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 0.5,
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
        self._range_complete = False
        self._traded_this_session = False
        self._exit_ordered = False

    def setup(self, ctx) -> None:
        # Guarantee a callback every 5 minutes even with no trades.
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

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
            self._range_complete = False
            self._traded_this_session = False
            self._exit_ordered = False

        # Build the opening range from bars in [RTH open, RTH open + 30m).
        if not self._range_complete:
            if bar.close_ts <= self._rth_open_ts:
                return

            if bar.open_ts < self._or_end_ts:
                # This is a range bar.  Include its traded high/low.
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
                # Never act on a range-defining bar.
                return

            # The first bar whose open_ts is at/after the range end means the
            # range is complete and this bar may be a signal bar.
            self._range_complete = True

        if not ctx.actionable:
            return

        # Manage the guaranteed session-time exit.  The engine bracket handles
        # the stop/target legs; this handles not holding overnight.
        if ctx.position:
            if bar.close_ts >= self._exit_ts and not self._exit_ordered:
                ctx.cancel_all()
                ctx.close()
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

        delta = bar.delta
        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        # Fade only when the breakout bar's signed volume disagrees with its
        # direction: an up-close on non-positive delta, or a down-close on
        # non-negative delta.
        if bar.close > or_high and delta <= 0:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-fade-short",
            )
        elif bar.close < or_low and delta >= 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-delta-fade-long",
            )
        else:
            order_id = None

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
