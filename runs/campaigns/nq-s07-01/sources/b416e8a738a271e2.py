"""Opening-range breakout: hold winning fades, cut stale strong trades.

Best to date is #153 (+0.2827). It keeps #150's 6-bar fade time stop and adds
an early loss cut for fades losing 0.30 * range by bar 3. #154 showed that
making the 6-bar fade cut conditional on being unprofitable is roughly a
neutral-to-positive choice, but #154 dropped the early loss cut and scored
slightly below #153.

This proposal keeps #153's profitable fade-side machinery exactly, makes the
6-bar fade cut conditional on the fade not showing a profit at or after bar 6
(the combined version that should score at least as well as #153), and adds a
new, analogous time-based hygiene cut for the *strong* side.

Strong-close breakouts are the "move holds" side. If a strong entry has not made
money after one hour (12 five-minute bars), the move has failed even though the
bracket stop is still far away. Cutting those stale strong positions before
their full 0.50 * range protective stop gives back less and mirrors the same
logic that made the fade-side time stop valuable. A strong position that is
profitable at the one-hour mark is allowed to continue toward its target.

Everything else remains unchanged from #153: 30-minute RTH opening range, 10%
breakout buffer, delta confirmation, close-strength split at 0.60, one trade per
session, fade_size=4, strong_size=1, fade bracket 0.45/0.25, strong bracket
0.50/0.50, the 15:00 CT flatten, and the 10-minute session-end margin.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


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
        strong_max_hold_bars: int = 12,
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
        self.strong_max_hold_bars = strong_max_hold_bars

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

        if not self._range_complete:
            if bar.close_ts <= self._rth_open_ts:
                return

            if bar.open_ts < self._or_end_ts:
                if bar.tick_count > 0:
                    if self._or_high is None or bar.high > self._or_high:
                        self._or_high = bar.high
                    if self._or_low is None or bar.low < self._or_low:
                        self._or_low = bar.low

                if bar.close_ts >= self._or_end_ts:
                    self._range_complete = True
                return

            self._range_complete = True

        if not ctx.actionable:
            return

        if ctx.position:
            self._position_age_bars += 1

            # Fast fade stop: cut losing fades before the wide protective stop.
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
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True

            # Stale-fade stop: at the 6-bar mark or later, cut unprofitable
            # fades. Profitable fades are working and are allowed to run.
            if (
                self._last_entry_was_fade
                and not self._exit_ordered
                and self._position_age_bars >= self.fade_max_hold_bars
                and ctx.unrealized_pnl <= 0.0
            ):
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True

            # Stale-strong stop: a breakout that has not made money after one
            # hour has failed; cut it before the full protective stop.
            if (
                not self._last_entry_was_fade
                and not self._exit_ordered
                and self._position_age_bars >= self.strong_max_hold_bars
                and ctx.unrealized_pnl <= 0.0
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
                self._fade_range = rng
                order_id = ctx.buy(
                    size=self.strong_size,
                    stop_offset=strong_stop,
                    target_offset=strong_target,
                    tag="or-strong-long",
                )
            else:
                self._last_entry_was_fade = True
                self._fade_range = rng
                order_id = ctx.sell(
                    size=self.fade_size,
                    stop_offset=fade_stop,
                    target_offset=fade_target,
                    tag="or-weak-fade-short",
                )

        elif bar.close < or_low - buffer and bar.delta < 0:
            if close_location <= (1.0 - self.close_strength):
                self._last_entry_was_fade = False
                self._fade_range = rng
                order_id = ctx.sell(
                    size=self.strong_size,
                    stop_offset=strong_stop,
                    target_offset=strong_target,
                    tag="or-strong-short",
                )
            else:
                self._last_entry_was_fade = True
                self._fade_range = rng
                order_id = ctx.buy(
                    size=self.fade_size,
                    stop_offset=fade_stop,
                    target_offset=fade_target,
                    tag="or-weak-fade-long",
                )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        self._position_age_bars = 0

    def on_lifecycle(self, ctx) -> None:
        pass
