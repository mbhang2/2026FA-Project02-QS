"""Opening-range breakout: profit-aware time stop for the profitable fade side.

Trial #150 scored +0.2812 by adding a flat 6-bar (30-minute) time stop to the
fade side, cutting every fade that had not hit its target by that age.  That
helped, but it also market-exits fades that are already in profit and may still
be travelling toward the target.

This proposal keeps the 6-bar time stop for fades that are losing or flat, but
treats a profitable fade differently: if a fade is in profit after 6 bars, the
reversion is working and the original target is still the right exit.  Instead
of closing it at market, we move the protective stop to a one-tick adverse
"breakeven" level and let the target work.  That preserves the downside control
of #150 while no longer leaving profitable fade targets on the table.

All other #150 parameters are unchanged: 30-minute RTH opening range, 10%
breakout buffer, delta confirmation, close-strength split at 0.60, one trade
per session, fade_size=4, strong_size=1, fade bracket 0.45 stop / 0.25 target,
strong bracket 0.50 stop / 0.50 target, and the 15:00 CT flatten.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Follow strong-close opening-range breakouts and fade weak-close ones."""

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
        strong_size: int = 1,
        fade_size: int = 4,
        strong_stop_mult: float = 0.5,
        strong_target_mult: float = 0.5,
        fade_stop_mult: float = 0.45,
        fade_target_mult: float = 0.25,
        min_range: float = 2.0,
        breakout_buffer_mult: float = 0.10,
        close_strength: float = 0.6,
        fade_max_hold_bars: int = 6,
        breakeven_offset: float = 0.25,
    ):
        self.interval = "5m"
        self.strong_size = strong_size
        self.fade_size = fade_size
        self.strong_stop_mult = strong_stop_mult
        self.strong_target_mult = strong_target_mult
        self.fade_stop_mult = fade_stop_mult
        self.fade_target_mult = fade_target_mult
        self.min_range = min_range
        self.breakout_buffer_mult = breakout_buffer_mult
        self.close_strength = close_strength
        self.fade_max_hold_bars = fade_max_hold_bars
        self.breakeven_offset = breakeven_offset

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
        self._position_age_bars: int = 0
        self._last_entry_was_fade: bool = False
        self._breakeven_armed: bool = False

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
            self._range_complete = False
            self._traded_this_session = False
            self._exit_ordered = False
            self._position_age_bars = 0
            self._last_entry_was_fade = False
            self._breakeven_armed = False

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

        # Managing an open position: time-stop stale fades, then mandatory
        # flatten before the session ends. The engine's bracket handles
        # stop/target; this handles exits that the bracket cannot express.
        if ctx.position:
            self._position_age_bars += 1

            if self._last_entry_was_fade and not self._exit_ordered:
                if self._position_age_bars >= self.fade_max_hold_bars:
                    if ctx.unrealized_pnl <= 0.0:
                        # A fade that is not in profit after its holding window
                        # is stale; cut it rather than waiting for the wider
                        # protective stop.
                        ctx.cancel_all()
                        close_id = ctx.close()
                        if close_id is not None:
                            self._exit_ordered = True
                    elif not self._breakeven_armed:
                        # A fade that is in profit after the holding window is
                        # already mean-reverting.  Tighten the protective stop
                        # to breakeven and let the original target work.
                        stops = ctx.find_orders(order_type="stop")
                        if stops:
                            entry = ctx.average_entry_price
                            if ctx.position < 0:
                                trigger = entry + self.breakeven_offset
                            else:
                                trigger = entry - self.breakeven_offset
                            ctx.modify(stops[0].id, trigger=trigger)
                            self._breakeven_armed = True

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

        strong_stop = rng * self.strong_stop_mult
        strong_target = rng * self.strong_target_mult
        fade_stop = rng * self.fade_stop_mult
        fade_target = rng * self.fade_target_mult

        order_id = None

        # Above the opening range, with delta confirming the up move.
        if bar.close > or_high + buffer and bar.delta > 0:
            if close_location >= self.close_strength:
                # Strong close: follow the breakout.
                self._last_entry_was_fade = False
                order_id = ctx.buy(
                    size=self.strong_size,
                    stop_offset=strong_stop,
                    target_offset=strong_target,
                    tag="or-strong-long",
                )
            else:
                # Weak close after an up break: fade it, with more size.
                self._last_entry_was_fade = True
                order_id = ctx.sell(
                    size=self.fade_size,
                    stop_offset=fade_stop,
                    target_offset=fade_target,
                    tag="or-weak-fade-short",
                )

        # Below the opening range, with delta confirming the down move.
        elif bar.close < or_low - buffer and bar.delta < 0:
            if close_location <= (1.0 - self.close_strength):
                # Strong close: follow the breakdown.
                self._last_entry_was_fade = False
                order_id = ctx.sell(
                    size=self.strong_size,
                    stop_offset=strong_stop,
                    target_offset=strong_target,
                    tag="or-strong-short",
                )
            else:
                # Weak close after a down break: fade it, with more size.
                self._last_entry_was_fade = True
                order_id = ctx.buy(
                    size=self.fade_size,
                    stop_offset=fade_stop,
                    target_offset=fade_target,
                    tag="or-weak-fade-long",
                )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        # Any fill restarts the time-stop clock.  A breakeven stop must be
        # re-armed for a fresh trade, never carried over from the previous one.
        self._position_age_bars = 0
        self._breakeven_armed = False

    def on_lifecycle(self, ctx) -> None:
        pass
