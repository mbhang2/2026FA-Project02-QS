"""Opening-range breakout: add strong-side breakeven stop to #174.

Best to date is #174 (+0.3208).  It adds a strong-side re-entry failure cut
(3 bars / back inside range) to #159's strong-side early loss cut and the
profitable fade-side logic of #153.

This proposal keeps #174 exactly and adds one strong-side protection: once a
strong-close breakout has moved at least 0.25 * opening range in its favor,
its protective stop is tightened to the average entry price.  A strong trade
that reaches this threshold and then reverses is scratched instead of being
allowed to return to a full 0.50 * range stop loss.  The tightening happens
only after the position is two 5-minute bars old, so the first bar's extreme
tick cannot trigger it on noise.

Fade-side logic, strong-side early loss cut, strong-side re-entry failure cut,
opening-range construction, brackets, time stops and the 15:00 CT flatten are
unchanged from #174.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND, OrderType


class Strategy:
    """Follow strong-close opening-range breakouts and fade weak-close ones."""

    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60
    OPENING_RANGE_SEC = 30 * 60
    EXIT_DELAY_SEC = 22 * 3600
    ENTRY_CUTOFF_DELAY_SEC = EXIT_DELAY_SEC - 30 * 60
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
        strong_breakeven_profit_mult: float = 0.25,
        strong_breakeven_min_bars: int = 2,
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
        self.fade_early_stop_bars = fade_early_stop_bars
        self.fade_early_stop_loss_mult = fade_early_stop_loss_mult
        self.strong_early_stop_bars = strong_early_stop_bars
        self.strong_early_stop_loss_mult = strong_early_stop_loss_mult
        self.strong_reentry_bars = strong_reentry_bars
        self.strong_breakeven_profit_mult = strong_breakeven_profit_mult
        self.strong_breakeven_min_bars = strong_breakeven_min_bars

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
        self._strong_breakeven_done: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

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
            self._strong_breakeven_done = False

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
                return

            self._range_complete = True

        if not ctx.actionable:
            return

        if ctx.position:
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

            strong_reentry_failure = False
            if (
                not self._last_entry_was_fade
                and not self._exit_ordered
                and self._fade_range is not None
                and self._or_high is not None
                and self._or_low is not None
                and self._position_age_bars >= self.strong_reentry_bars
            ):
                if ctx.position > 0 and bar.close < self._or_high:
                    strong_reentry_failure = True
                elif ctx.position < 0 and bar.close > self._or_low:
                    strong_reentry_failure = True

            if fade_early_stop or strong_early_stop or strong_reentry_failure:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True

            # New: strong-side breakeven stop.  Once a strong breakout has
            # moved far enough in our favor, raise the protective stop to the
            # average entry price so a subsequent round trip scratches instead
            # of losing.
            if (
                not self._last_entry_was_fade
                and not self._exit_ordered
                and not self._strong_breakeven_done
                and self._fade_range is not None
                and self._entry_order_id is not None
                and self._position_age_bars >= self.strong_breakeven_min_bars
            ):
                if ctx.position > 0:
                    favorable_move = ctx.last_price - ctx.average_entry_price
                elif ctx.position < 0:
                    favorable_move = ctx.average_entry_price - ctx.last_price
                else:
                    favorable_move = 0.0

                if (
                    favorable_move
                    >= self._fade_range * self.strong_breakeven_profit_mult
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
                        if (
                            self._entry_is_long
                            and breakeven > current_trigger
                            and breakeven < ctx.last_price
                        ):
                            ctx.modify(stop_order.id, trigger=breakeven)
                        elif (
                            not self._entry_is_long
                            and breakeven < current_trigger
                            and breakeven > ctx.last_price
                        ):
                            ctx.modify(stop_order.id, trigger=breakeven)
                    self._strong_breakeven_done = True

            if (
                self._last_entry_was_fade
                and not self._exit_ordered
                and self._position_age_bars >= self.fade_max_hold_bars
            ):
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True

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

        if bar.close > or_high + buffer and bar.delta > 0:
            if close_location >= self.close_strength:
                self._last_entry_was_fade = False
                self._entry_is_long = True
                self._fade_range = rng
                order_id = ctx.buy(
                    size=self.strong_size,
                    stop_offset=strong_stop,
                    target_offset=strong_target,
                    tag="or-strong-long",
                )
                if order_id is not None:
                    self._entry_order_id = order_id
                    self._strong_breakeven_done = False
            else:
                self._last_entry_was_fade = True
                self._entry_is_long = False
                self._fade_range = rng
                order_id = ctx.sell(
                    size=self.fade_size,
                    stop_offset=fade_stop,
                    target_offset=fade_target,
                    tag="or-weak-fade-short",
                )
                if order_id is not None:
                    self._entry_order_id = order_id
                    self._strong_breakeven_done = False

        elif bar.close < or_low - buffer and bar.delta < 0:
            if close_location <= (1.0 - self.close_strength):
                self._last_entry_was_fade = False
                self._entry_is_long = False
                self._fade_range = rng
                order_id = ctx.sell(
                    size=self.strong_size,
                    stop_offset=strong_stop,
                    target_offset=strong_target,
                    tag="or-strong-short",
                )
                if order_id is not None:
                    self._entry_order_id = order_id
                    self._strong_breakeven_done = False
            else:
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
                    self._entry_order_id = order_id
                    self._strong_breakeven_done = False

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        self._position_age_bars = 0
        if ctx.fill.position == 0:
            self._entry_order_id = None
            self._entry_is_long = False
            self._strong_breakeven_done = False

    def on_lifecycle(self, ctx) -> None:
        pass
