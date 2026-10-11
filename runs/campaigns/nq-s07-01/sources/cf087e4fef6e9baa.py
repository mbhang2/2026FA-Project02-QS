"""Opening-range breakout with flow-alignment fade.

This keeps #41's profitable structure exactly for the strong-close,
flow-aligned breakout: a 30-minute regular-hours opening range, 10% buffer,
5-minute bars, half-range symmetric bracket, one trade per session, 15:00 CT
flatten and 14:30 CT entry cutoff.

The change is a new side rule that uses the opening range itself rather than
only the close location of the breakout bar.  #41 fades a breakout when the
bar that breaks out closes weakly, but follows any strong-close breakout whose
bar delta has the breakout direction.  Here I add one more "range wins"
condition:

  * If a strong-close upside breakout occurs after an opening range with
    strongly negative order flow (buy_volume - sell_volume, divided by
    volume), the upside break is against the grain of the range.  A bar can
    close at its high and still be a short-covering spike that gets faded; the
    opening range delta is the evidence for that.  I fade it instead of
    following it.
  * Symmetrically, a strong-close downside breakout after a strongly positive
    opening range is faded.

The condition is a fixed rule, not a fitted regime.  Weak-close breakouts are
faded exactly as in #41.  The strong-close follow trades that do *not* have a
strongly opposing range delta are unchanged, so most of #41's behavior is
preserved and the new rule only flips the high-conviction disagreement cases.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Follow flow-aligned strong breakouts; fade weak and flow-opposed ones."""

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
        range_delta_fade_frac: float = 0.20,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_range = min_range
        self.breakout_buffer_mult = breakout_buffer_mult
        self.close_strength = close_strength
        self.range_delta_fade_frac = range_delta_fade_frac

        self._session: int = -1
        self._rth_open_ts: int = 0
        self._or_end_ts: int = 0
        self._entry_cutoff_ts: int = 0
        self._exit_ts: int = 0
        self._or_high: Optional[float] = None
        self._or_low: Optional[float] = None
        self._or_volume: int = 0
        self._or_delta: int = 0
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
            self._exit_ts = min(
                ctx.session_start + self.EXIT_DELAY_SEC * NS_PER_SECOND,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            # Never allow a new entry after the forced exit time; on a half-day
            # session the scheduled 14:30 CT cutoff would otherwise be later
            # than the exit, and entering in that gap could create an overnight
            # hold with no later callback to flatten it.
            self._entry_cutoff_ts = min(
                ctx.session_start + self.ENTRY_CUTOFF_DELAY_SEC * NS_PER_SECOND,
                self._exit_ts,
            )
            self._or_high = None
            self._or_low = None
            self._or_volume = 0
            self._or_delta = 0
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
                    self._or_volume += bar.volume
                    self._or_delta += bar.delta

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

        if self._or_volume > 0:
            range_delta_frac = self._or_delta / self._or_volume
        else:
            range_delta_frac = 0.0

        buffer = rng * self.breakout_buffer_mult
        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        order_id = None

        # Above the opening range, with delta confirming the up move.
        if bar.close > or_high + buffer and bar.delta > 0:
            if close_location >= self.close_strength:
                # Strong close.  Follow if the opening-range flow does not
                # strongly oppose the break; otherwise fade it.
                if range_delta_frac < -self.range_delta_fade_frac:
                    order_id = ctx.sell(
                        size=self.size,
                        stop_offset=stop_dist,
                        target_offset=target_dist,
                        tag="or-flow-fade-short",
                    )
                else:
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
                # Strong close.  Follow if the opening-range flow does not
                # strongly oppose the break; otherwise fade it.
                if range_delta_frac > self.range_delta_fade_frac:
                    order_id = ctx.buy(
                        size=self.size,
                        stop_offset=stop_dist,
                        target_offset=target_dist,
                        tag="or-flow-fade-long",
                    )
                else:
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
