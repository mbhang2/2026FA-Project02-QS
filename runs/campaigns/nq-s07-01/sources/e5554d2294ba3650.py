"""Opening-range breakout: follow high-conviction strong closes, fade weak ones.

The best prior strategy (#41) follows strong-close delta-confirmed opening-range
breakouts and fades weak-close ones.  It scored +0.1970.  This proposal keeps
its opening-range construction, 10% buffer, delta confirmation, half-range
bracket, 15:00 CT flatten and 14:30 CT entry cutoff exactly.

It adds one refinement to the "move holds" branch only.  A strong-close
breakout is followed only when it also clears two higher-conviction tests:
the breakout bar closes in the direction of the move (`close > open` for an
up break, `close < open` for a down break), and its volume is at least the
average volume of the opening-range bars.  The "range wins" fade branch is
unchanged: a delta-confirmed breakout bar that closes weakly through the level
was already retracing the breakout, and that is the range-winning outcome.

This makes the follow side more selective without reducing the fade side,
while still acting on the ordinary run of sessions.

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
        it is a strong-close breakout.  Follow it with a buy only when
        `bar.close > bar.open` and volume is at least
        `follow_volume_mult * average_opening_range_volume`.
      * otherwise sell (the range wins: it broke out and reversed).
    For a bar below the range:
      * if its close is in the bottom `close_strength` fraction of its body,
        follow with a sell only when `bar.close < bar.open` and the volume
        expansion test passes.
      * otherwise buy.

    One trade per session.

Exit:
    Entry is protected immediately by the engine's bracket, with stop and
    target offsets both equal to half the opening-range height.  Any open
    position is flattened by `15:00 CT` or earlier on short sessions.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Follow high-conviction strong-close opening-range breakouts and fade weak ones."""

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
        target_mult: float = 0.5,
        min_range: float = 2.0,
        breakout_buffer_mult: float = 0.1,
        close_strength: float = 0.6,
        follow_volume_mult: float = 1.0,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.breakout_buffer_mult = breakout_buffer_mult
        self.close_strength = close_strength
        self.follow_volume_mult = follow_volume_mult

        self._session: int = -1
        self._rth_open_ts: int = 0
        self._or_end_ts: int = 0
        self._entry_cutoff_ts: int = 0
        self._exit_ts: int = 0
        self._or_high: Optional[float] = None
        self._or_low: Optional[float] = None
        self._or_vol_sum: float = 0.0
        self._or_vol_count: int = 0
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
            self._or_high = None
            self._or_low = None
            self._or_vol_sum = 0.0
            self._or_vol_count = 0
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

                    self._or_vol_sum += bar.volume
                    self._or_vol_count += 1

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
        target_dist = rng * self.target_mult

        or_avg_volume = (
            self._or_vol_sum / self._or_vol_count
            if self._or_vol_count > 0
            else 0.0
        )

        order_id = None

        # Above the opening range, with delta confirming the up move.
        if bar.close > or_high + buffer and bar.delta > 0:
            if close_location >= self.close_strength:
                # Strong close: follow the breakout only with high conviction.
                if (
                    bar.close > bar.open
                    and bar.volume >= self.follow_volume_mult * or_avg_volume
                ):
                    order_id = ctx.buy(
                        size=self.size,
                        stop_offset=stop_dist,
                        target_offset=target_dist,
                        tag="or-strong-long",
                    )
            else:
                # Weak close after an up break: fade it.
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-weak-fade-short",
                )

        # Below the opening range, with delta confirming the down move.
        elif bar.close < or_low - buffer and bar.delta < 0:
            if close_location <= (1.0 - self.close_strength):
                # Strong close: follow the breakdown only with high conviction.
                if (
                    bar.close < bar.open
                    and bar.volume >= self.follow_volume_mult * or_avg_volume
                ):
                    order_id = ctx.sell(
                        size=self.size,
                        stop_offset=stop_dist,
                        target_offset=target_dist,
                        tag="or-strong-short",
                    )
            else:
                # Weak close after a down break: fade it.
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_dist,
                    target_offset=target_dist,
                    tag="or-weak-fade-long",
                )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
