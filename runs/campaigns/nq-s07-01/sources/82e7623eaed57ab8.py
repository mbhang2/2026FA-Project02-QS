"""Opening-range breakout: #182 plus a breakeven hold for profitable fades.

Best to date is #182 (+0.3878), which cuts strong positions on the first
30-second close back inside the opening range once the position is three
5-minute bars old. #180/#183 showed that at the six-bar fade time stop a
profitable fade benefits from being held with its protective stop tightened to
breakeven instead of being market-closed: on the 1m-cut base that added about
+0.0034 (#177 -> #180).

This proposal applies exactly that profitable-fade breakeven hold on top of
#182, keeping the 30-second re-entry failure cut. Strong-side logic, fade-side
early loss cut, opening-range construction, brackets, one-trade-per-session,
time stops and the 15:00 CT flatten are unchanged.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND, OrderType


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
        fade_early_stop_bars: int = 3,
        fade_early_stop_loss_mult: float = 0.30,
        strong_early_stop_bars: int = 6,
        strong_early_stop_loss_mult: float = 0.40,
        strong_reentry_bars: int = 3,
    ):
        self.interval = "5m"
        self.reentry_interval = "30s"
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
        self.fade_early_stop_bars = fade_early_stop_bars
        self.fade_early_stop_loss_mult = fade_early_stop_loss_mult
        self.strong_early_stop_bars = strong_early_stop_bars
        self.strong_early_stop_loss_mult = strong_early_stop_loss_mult
        self.strong_reentry_bars = strong_reentry_bars

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
        self._fade_range: Optional[float] = None
        self._entry_order_id: Optional[int] = None
        self._entry_is_long: bool = False
        self._fade_time_stop_resolved: bool = False

    def setup(self, ctx) -> None:
        # The decision interval and a finer interval for the earlier strong-side
        # re-entry failure cut.  Both are time bars, so the extra 30s
        # subscription keeps the engine event-driven.
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.reentry_interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        bar5 = ctx.bars.get(self.interval)
        bar30 = ctx.bars.get(self.reentry_interval)
        if bar5 is None and bar30 is None:
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
            self._fade_range = None
            self._entry_order_id = None
            self._entry_is_long = False
            self._fade_time_stop_resolved = False

        # Build the opening range from 5-minute bars between RTH open and
        # RTH open + 30 minutes.  30-second bars are ignored before the range
        # is complete.
        if not self._range_complete:
            if bar5 is None:
                return
            bar = bar5

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

        # Managing an open position.
        if ctx.position:
            self._manage_position(ctx, bar5, bar30)
            return

        # Flat: only act on 5-minute decision bars.
        if self._traded_this_session:
            return
        if bar5 is None:
            return
        if bar5.close_ts >= self._entry_cutoff_ts:
            return
        if bar5.tick_count == 0:
            return

        or_high = self._or_high
        or_low = self._or_low
        if or_high is None or or_low is None:
            return

        rng = or_high - or_low
        if rng < self.min_range:
            return

        body = bar5.high - bar5.low
        if body <= 0:
            return
        close_location = (bar5.close - bar5.low) / body

        buffer = rng * self.breakout_buffer_mult

        strong_stop = rng * self.strong_stop_mult
        strong_target = rng * self.strong_target_mult
        fade_stop = rng * self.fade_stop_mult
        fade_target = rng * self.fade_target_mult

        order_id = None

        # Above the opening range, with delta confirming the up move.
        if bar5.close > or_high + buffer and bar5.delta > 0:
            if close_location >= self.close_strength:
                # Strong close: follow the breakout.
                self._last_entry_was_fade = False
                self._entry_is_long = True
                self._fade_range = rng
                order_id = ctx.buy(
                    size=self.strong_size,
                    stop_offset=strong_stop,
                    target_offset=strong_target,
                    tag="or-strong-long",
                )
            else:
                # Weak close after an up break: fade it, with more size and
                # an exit profile designed for "the range wins".
                self._last_entry_was_fade = True
                self._entry_is_long = False
                self._fade_range = rng
                order_id = ctx.sell(
                    size=self.fade_size,
                    stop_offset=fade_stop,
                    target_offset=fade_target,
                    tag="or-weak-fade-short",
                )

        # Below the opening range, with delta confirming the down move.
        elif bar5.close < or_low - buffer and bar5.delta < 0:
            if close_location <= (1.0 - self.close_strength):
                # Strong close: follow the breakdown.
                self._last_entry_was_fade = False
                self._entry_is_long = False
                self._fade_range = rng
                order_id = ctx.sell(
                    size=self.strong_size,
                    stop_offset=strong_stop,
                    target_offset=strong_target,
                    tag="or-strong-short",
                )
            else:
                # Weak close after a down break: fade it, with more size.
                self._last_entry_was_fade = True
                self._entry_is_long = True
                self._fade_range = rng
                order_id = ctx.buy(
                    size=self.fade_size,
                    stop_offset=fade_stop,
                    target_offset=fade_target,
                    tag="or-weak-fade-long",
                )

        if order_id is not None:
            self._traded_this_session = True
            self._entry_order_id = order_id
            self._fade_time_stop_resolved = False

    def _manage_position(self, ctx, bar5, bar30) -> None:
        """Manage an open position.

        The original 5-minute management rules run only when a 5-minute bar has
        closed.  The strong-side re-entry failure cut may run on either a
        5-minute or a 30-second bar close, and exits on the first non-empty
        closed bar back inside the opening range once the position is old enough.

        New in this proposal: at the fade time stop, a profitable fade is held
        with its protective stop tightened to breakeven instead of being closed.
        """
        if bar5 is not None:
            self._position_age_bars += 1

            # Fade-side early loss cut: if a fade has already lost 0.30 * range
            # after three bars, cut it before the 0.45 * range protective stop
            # gives back more than necessary.
            fade_early_stop = False
            if (
                self._last_entry_was_fade
                and not self._exit_ordered
                and self._fade_range is not None
                and self._position_age_bars >= self.fade_early_stop_bars
                and self._position_age_bars < self.fade_max_hold_bars
                and ctx.unrealized_pnl
                <= -(
                    self.fade_early_stop_loss_mult
                    * self._fade_range
                    * abs(ctx.position)
                )
            ):
                fade_early_stop = True

            # Strong-side early loss cut: a strong-close breakout should not be
            # losing 0.40 * range after six bars if the move is holding.  Cut it
            # before it reaches the 0.50 * range protective stop.
            strong_early_stop = False
            if (
                not self._last_entry_was_fade
                and not self._exit_ordered
                and self._fade_range is not None
                and self._position_age_bars >= self.strong_early_stop_bars
                and ctx.unrealized_pnl
                <= -(
                    self.strong_early_stop_loss_mult
                    * self._fade_range
                    * abs(ctx.position)
                )
            ):
                strong_early_stop = True

            if fade_early_stop or strong_early_stop:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True

            # Fade time-stop rule.  #182 would market-close every fade still
            # open at the six-bar boundary.  Here a profitable fade is allowed
            # to continue toward its original target, with its protective stop
            # tightened to breakeven.
            if (
                self._last_entry_was_fade
                and not self._exit_ordered
                and not self._fade_time_stop_resolved
                and self._position_age_bars >= self.fade_max_hold_bars
            ):
                if ctx.unrealized_pnl > 0:
                    if (
                        self._fade_range is not None
                        and self._entry_order_id is not None
                    ):
                        stops = ctx.find_orders(
                            parent_id=self._entry_order_id,
                            order_type=OrderType.STOP,
                        )
                        breakeven = ctx.average_entry_price
                        for stop_order in stops:
                            current_trigger = stop_order.stop_price
                            if current_trigger is None:
                                continue
                            if self._entry_is_long:
                                # Long's protective stop is a sell stop below
                                # the entry; raise it to entry.
                                if breakeven > current_trigger:
                                    ctx.modify(stop_order.id, trigger=breakeven)
                            else:
                                # Short's protective stop is a buy stop above
                                # the entry; lower it to entry.
                                if breakeven < current_trigger:
                                    ctx.modify(stop_order.id, trigger=breakeven)
                    self._fade_time_stop_resolved = True
                else:
                    ctx.cancel_all()
                    close_id = ctx.close()
                    if close_id is not None:
                        self._exit_ordered = True
                        self._fade_time_stop_resolved = True

            if bar5.close_ts >= self._exit_ts and not self._exit_ordered:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True

        # Strong-side re-entry failure cut.  Once the position has been alive
        # for three 5-minute bars, use the first available non-empty closed bar
        # (prefer the 30-second bar when both are present) to detect a close
        # back inside the opening range.
        if (
            not self._exit_ordered
            and not self._last_entry_was_fade
            and self._fade_range is not None
            and self._or_high is not None
            and self._or_low is not None
            and self._position_age_bars >= self.strong_reentry_bars
        ):
            reentry_bar = None
            if bar30 is not None and bar30.tick_count > 0:
                reentry_bar = bar30
            elif bar5 is not None and bar5.tick_count > 0:
                reentry_bar = bar5

            if reentry_bar is not None:
                if ctx.position > 0:
                    inside = reentry_bar.close < self._or_high
                elif ctx.position < 0:
                    inside = reentry_bar.close > self._or_low
                else:
                    inside = False

                if inside:
                    ctx.cancel_all()
                    close_id = ctx.close()
                    if close_id is not None:
                        self._exit_ordered = True

    def on_fill(self, ctx) -> None:
        # Any fill restarts the holding-window clock. A new entry gets a fresh
        # holding window; a flattening exit clears the position-specific state.
        self._position_age_bars = 0
        if ctx.fill.position == 0:
            self._entry_order_id = None
            self._entry_is_long = False
            self._fade_time_stop_resolved = False

    def on_lifecycle(self, ctx) -> None:
        pass
