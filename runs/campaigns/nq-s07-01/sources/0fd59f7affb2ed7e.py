"""Opening-range short fade.

The campaign asks whether a move out of the regular-hours opening range holds
or the range wins. This strategy tests the asymmetric "range wins on the
upside" account: it only fades upward breakouts. Downside breakouts are left
alone. Prior evidence in this campaign family pointed at the short side of
opening-range fades, so isolating that side should tell us whether the
symmetric fade's long side was the drag.

Opening range:
    high and low of the first 30 minutes of regular-hours trading
    (08:30 CT to 09:00 CT), measured on 5-minute bars. The regular-hours open
    is 15.5 hours after the CME Globex session open (17:00 CT), so it is a
    fixed bar offset with no lookahead.

Signal:
    After the range is known, the first completed 5-minute bar that closes
    above the opening-range high triggers a market sell. One trade per
    session.

Exit:
    The engine's bracket protects the entry. Stop and target offsets are both
    half of the opening-range height, so the target is a return toward the
    range midpoint. The position is flattened by 15:00 CT, or earlier if the
    session closes early.
"""

from __future__ import annotations

from src.core.types import NS_PER_SECOND


class Strategy:
    """Fade the first upside break of the regular-hours opening range."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Flatten by 15:00 CT, giving a full hour before the 16:00 CT session close.
    RTH_EXIT_DELAY_SEC = 22 * 3600

    # Do not open a new trade in the last 30 minutes before the scheduled exit.
    ENTRY_CUTOFF_DELAY_SEC = RTH_EXIT_DELAY_SEC - 30 * 60

    # If a session ends early, force the exit this long before its close.
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

        self._session = None
        self._range_high = None
        self._range_low = None
        self._traded_this_session = False

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
            self._traded_this_session = False

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
            return

        if not ctx.actionable:
            return

        if self._range_high is None or self._range_low is None:
            return

        # Manage the session-time exit while in a position.
        if ctx.position:
            exit_time = min(
                ctx.session_start + self.RTH_EXIT_DELAY_SEC * NS_PER_SECOND,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            if bar.close_ts >= exit_time:
                ctx.cancel_all()
                ctx.close()
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

        # Trade only the upside breakout. Downside breakouts are deliberately
        # left alone for this trial.
        if bar.close > self._range_high:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_mult * rng,
                target_offset=self.target_mult * rng,
                tag="or-short-fade",
            )
            if order_id is not None:
                self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
