"""Opening-range breakout: #296 with the fade target schedule pushed one more step.

#296 is the best so far (0.6073). It maps close weakness to a fade target scale
of 0.38 + 1.10 * weakness:
- weakness 0.4 -> 0.82 * base
- weakness 0.7 -> 1.15 * base
- weakness 1.0 -> 1.48 * base

This proposal keeps the mild end anchored at the same 0.82 * base and continues
the same direction that improved #292 -> #296: lower the floor and raise the
slope. The new schedule is 0.36 + 1.15 * weakness:
- weakness 0.4 -> 0.82 * base (unchanged)
- weakness 0.7 -> 1.165 * base (versus 1.15 in #296)
- weakness 1.0 -> 1.51 * base (versus 1.48 in #296)

The cleanest "range wins" fades are rewarded a little more, while the milder,
less certain fades are left unchanged. Everything else is unchanged from #296.
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
        fade_size: int = 16,
        fade_size_min_mult: float = 0.6,
        fade_size_max_mult: float = 1.4,
        fade_size_weakness_lo: float = 0.4,
        strong_stop_mult: float = 0.5,
        strong_target_mult: float = 0.5,
        fade_stop_mult: float = 0.45,
        fade_target_mult: float = 0.25,
        min_range: float = 0.0,
        breakout_buffer_mult: float = 0.10,
        close_strength: float = 0.6,
        fade_max_hold_bars: int = 6,
        fade_early_stop_bars: int = 3,
        fade_early_stop_loss_mult: float = 0.30,
        strong_early_stop_bars: int = 6,
        strong_early_stop_loss_mult: float = 0.40,
        strong_reentry_bars: int = 3,
        fade_fine_warmup_sec: int = 60,
        strong_fine_warmup_sec: int = 90,
        strong_time_stop_bars: int = 6,
        fade_target_weakness_floor: float = 0.36,
        fade_target_weakness_slope: float = 1.15,
    ):
        self.interval = "5m"
        self.reentry_interval = "10s"
        self.strong_size = strong_size
        self.fade_size = fade_size
        self.fade_size_min_mult = fade_size_min_mult
        self.fade_size_max_mult = fade_size_max_mult
        self.fade_size_weakness_lo = fade_size_weakness_lo
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
        self.fade_fine_warmup_sec = fade_fine_warmup_sec
        self.strong_fine_warmup_sec = strong_fine_warmup_sec
        self.strong_time_stop_bars = strong_time_stop_bars
        self.fade_target_weakness_floor = fade_target_weakness_floor
        self.fade_target_weakness_slope = fade_target_weakness_slope

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
        self._strong_time_stop_resolved: bool = False
        self._entry_fill_ts: Optional[int] = None
        self._short_session: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)
        ctx.subscribe(self.reentry_interval, emit_empty=False)

    def _fade_size_for_weakness(self, weakness: float) -> int:
        """Map close weakness to a fade size.

        #267's wider band: weakness 0.4 -> 0.6 * fade_size, weakness 1.0 ->
        1.4 * fade_size. The midpoint at weakness 0.7 remains 1.0, so the
        average fade size stays near `fade_size` while the extremes receive
        more separation than #263.
        """
        weakness = min(1.0, max(self.fade_size_weakness_lo, weakness))
        span = self.fade_size_max_mult - self.fade_size_min_mult
        multiplier = self.fade_size_min_mult + span * (
            (weakness - self.fade_size_weakness_lo)
            / (1.0 - self.fade_size_weakness_lo)
        )
        return int(round(self.fade_size * multiplier))

    def on_bars(self, ctx) -> None:
        bar5 = ctx.bars.get(self.interval)
        bar10 = ctx.bars.get(self.reentry_interval)
        if bar5 is None and bar10 is None:
            return

        # New session: reset session-local state and precompute fixed times.
        if ctx.session != self._session:
            self._session = ctx.session
            session_duration = ctx.session_end - ctx.session_start
            short_session = session_duration < (
                self.RTH_OPEN_DELAY_SEC + self.OPENING_RANGE_SEC + 30 * 60
            ) * NS_PER_SECOND

            if short_session:
                self._short_session = True
                self._rth_open_ts = ctx.session_start
                self._or_end_ts = min(
                    ctx.session_start + self.OPENING_RANGE_SEC * NS_PER_SECOND,
                    ctx.session_end - NS_PER_SECOND,
                )
                self._entry_cutoff_ts = max(
                    ctx.session_start, ctx.session_end - 5 * 60 * NS_PER_SECOND
                )
                self._exit_ts = max(
                    ctx.session_start, ctx.session_end - 2 * 60 * NS_PER_SECOND
                )
            else:
                self._short_session = False
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
            self._strong_time_stop_resolved = False
            self._entry_fill_ts = None

        # Build the opening range from 5-minute bars between RTH open and
        # RTH open + 30 minutes.  10-second bars are ignored before the range
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
                return

            self._range_complete = True

        if not ctx.actionable:
            return

        # Managing an open position.
        if ctx.position:
            self._manage_position(ctx, bar5, bar10)
            return

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
        if rng <= 0:
            return

        effective_min_range = 0.0 if self._short_session else self.min_range
        if rng < effective_min_range:
            return

        body = bar5.high - bar5.low
        if body <= 0:
            return
        close_location = (bar5.close - bar5.low) / body

        if self._short_session:
            buffer = 0.0
        else:
            buffer = rng * self.breakout_buffer_mult

        strong_stop = rng * self.strong_stop_mult
        strong_target = rng * self.strong_target_mult
        fade_stop = rng * self.fade_stop_mult
        fade_target_base = rng * self.fade_target_mult

        order_id = None

        # Above the opening range, with delta confirming the up move.
        if bar5.close > or_high + buffer and (
            self._short_session or bar5.delta > 0
        ):
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
                # Weak close after an up break: fade it, with size and target
                # both scaled by how weak the close is.
                weakness = 1.0 - close_location
                fade_size_used = self._fade_size_for_weakness(weakness)
                target_scale = (
                    self.fade_target_weakness_floor
                    + self.fade_target_weakness_slope * weakness
                )
                fade_target_used = fade_target_base * target_scale
                self._last_entry_was_fade = True
                self._entry_is_long = False
                self._fade_range = rng
                order_id = ctx.sell(
                    size=fade_size_used,
                    stop_offset=fade_stop,
                    target_offset=fade_target_used,
                    tag="or-weak-fade-short",
                )

        # Below the opening range, with delta confirming the down move.
        elif bar5.close < or_low - buffer and (
            self._short_session or bar5.delta < 0
        ):
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
                # Weak close after a down break: fade it, with size and target
                # both scaled by close weakness.
                weakness = close_location
                fade_size_used = self._fade_size_for_weakness(weakness)
                target_scale = (
                    self.fade_target_weakness_floor
                    + self.fade_target_weakness_slope * weakness
                )
                fade_target_used = fade_target_base * target_scale
                self._last_entry_was_fade = True
                self._entry_is_long = True
                self._fade_range = rng
                order_id = ctx.buy(
                    size=fade_size_used,
                    stop_offset=fade_stop,
                    target_offset=fade_target_used,
                    tag="or-weak-fade-long",
                )

        if order_id is not None:
            self._traded_this_session = True
            self._entry_order_id = order_id
            self._fade_time_stop_resolved = False
            self._strong_time_stop_resolved = False

    def _manage_position(self, ctx, bar5, bar10) -> None:
        """Manage an open position."""
        if bar5 is not None:
            self._position_age_bars += 1

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
                                if breakeven > current_trigger:
                                    ctx.modify(stop_order.id, trigger=breakeven)
                            else:
                                if breakeven < current_trigger:
                                    ctx.modify(stop_order.id, trigger=breakeven)
                    self._fade_time_stop_resolved = True
                else:
                    ctx.cancel_all()
                    close_id = ctx.close()
                    if close_id is not None:
                        self._exit_ordered = True
                        self._fade_time_stop_resolved = True

            if (
                not self._last_entry_was_fade
                and not self._exit_ordered
                and not self._strong_time_stop_resolved
                and self._position_age_bars >= self.strong_time_stop_bars
            ):
                if ctx.unrealized_pnl > 0:
                    if self._entry_order_id is not None:
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
                                if breakeven > current_trigger:
                                    ctx.modify(stop_order.id, trigger=breakeven)
                            else:
                                if breakeven < current_trigger:
                                    ctx.modify(stop_order.id, trigger=breakeven)
                    self._strong_time_stop_resolved = True
                else:
                    ctx.cancel_all()
                    close_id = ctx.close()
                    if close_id is not None:
                        self._exit_ordered = True
                        self._strong_time_stop_resolved = True

            if bar5.close_ts >= self._exit_ts and not self._exit_ordered:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True

        fine_bar = None
        if bar10 is not None and bar10.tick_count > 0:
            fine_bar = bar10
        elif bar5 is not None and bar5.tick_count > 0:
            fine_bar = bar5

        if (
            self._last_entry_was_fade
            and not self._exit_ordered
            and self._fade_range is not None
            and fine_bar is not None
            and self._position_age_bars < self.fade_max_hold_bars
        ):
            warmup_elapsed = (
                self._entry_fill_ts is not None
                and ctx.ts - self._entry_fill_ts
                >= self.fade_fine_warmup_sec * NS_PER_SECOND
            )
            if self._position_age_bars >= 1 or warmup_elapsed:
                adverse_px = self._fade_range * self.fade_early_stop_loss_mult
                if self._entry_is_long:
                    if fine_bar.close < ctx.average_entry_price - adverse_px:
                        ctx.cancel_all()
                        close_id = ctx.close()
                        if close_id is not None:
                            self._exit_ordered = True
                else:
                    if fine_bar.close > ctx.average_entry_price + adverse_px:
                        ctx.cancel_all()
                        close_id = ctx.close()
                        if close_id is not None:
                            self._exit_ordered = True

        if (
            not self._last_entry_was_fade
            and not self._exit_ordered
            and self._fade_range is not None
            and fine_bar is not None
            and self._position_age_bars < self.strong_early_stop_bars
        ):
            strong_warmup_elapsed = (
                self._entry_fill_ts is not None
                and ctx.ts - self._entry_fill_ts
                >= self.strong_fine_warmup_sec * NS_PER_SECOND
            )
            if strong_warmup_elapsed:
                adverse_px = self._fade_range * self.strong_early_stop_loss_mult
                if self._entry_is_long:
                    if fine_bar.close < ctx.average_entry_price - adverse_px:
                        ctx.cancel_all()
                        close_id = ctx.close()
                        if close_id is not None:
                            self._exit_ordered = True
                else:
                    if fine_bar.close > ctx.average_entry_price + adverse_px:
                        ctx.cancel_all()
                        close_id = ctx.close()
                        if close_id is not None:
                            self._exit_ordered = True

        if (
            not self._exit_ordered
            and not self._last_entry_was_fade
            and self._fade_range is not None
            and self._or_high is not None
            and self._or_low is not None
            and self._position_age_bars >= self.strong_reentry_bars
        ):
            if fine_bar is not None:
                if ctx.position > 0:
                    inside = fine_bar.close < self._or_high
                elif ctx.position < 0:
                    inside = fine_bar.close > self._or_low
                else:
                    inside = False

                if inside:
                    ctx.cancel_all()
                    close_id = ctx.close()
                    if close_id is not None:
                        self._exit_ordered = True

    def on_fill(self, ctx) -> None:
        self._position_age_bars = 0
        if ctx.fill.position == 0:
            self._entry_order_id = None
            self._entry_is_long = False
            self._fade_time_stop_resolved = False
            self._strong_time_stop_resolved = False
            self._entry_fill_ts = None
        else:
            if self._entry_fill_ts is None:
                self._entry_fill_ts = ctx.ts
            self._strong_time_stop_resolved = False

    def on_lifecycle(self, ctx) -> None:
        pass
