"""Opening range fade using true prior-day RTH volatility.

This is #92 with one deliberate and substantive change: the volatility scale
used for the breakout threshold and the bracket is a trailing average true
range computed only from prior completed regular-hours sessions, not from the
current session's Globex/overnight bars.

#92 clears its true-range buffer at each session start, but by the time the
entry window opens (20 minutes after the 30-minute opening range is ready)
the buffer has already been refilled with roughly 930 Globex one-minute bars
plus the early RTH bars.  Its ATR is therefore overwhelmingly a current-
session overnight measurement.  #134 attempted to fix this by not clearing
the buffer across sessions, but the trailing 150 one-minute bars used for the
ATR were still all from the current session, so the strategy's decisions were
identical.

Here the regular-hours one-minute true ranges are accumulated separately in
`rth_true_ranges`.  At each new session, the ATR is fixed once as the average
of the last `vol_lookback` regular-hours true ranges from *previous* sessions.
Current-session bars are not allowed to affect it.  This makes the definition
of "leaving" comparable across calm and loud sessions using prior-day trailing
realized volatility, as the campaign requires.  Until enough prior RTH bars
exist (the first session or two), the strategy falls back to the #92 current-
session ATR so it does not sit out a large part of the scored split.

Everything else is retained from #92: the 30-minute RTH opening range, the
within-bar same-direction order-flow fraction filter, the absolute-delta
filter for unusually large directional flow, the minimum range-width filter,
the non-volatile breakout bar filter, the first-qualifying-breakout rule, the
20-110 minute entry window after the range is ready, and the 0.8/3.0 ATR
stop/target bracket.  Positions are also time-exited after 100 minutes or
5 minutes before session end.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from src.core.types import NS_PER_SECOND

NS_PER_MINUTE = 60 * NS_PER_SECOND
NS_PER_HOUR = 3600 * NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_range_minutes: int = 30,
        vol_lookback: int = 150,
        breakout_vol_mult: float = 0.8,
        stop_vol_mult: float = 0.8,
        target_vol_mult: float = 3.0,
        max_hold_minutes: int = 100,
        min_wait_after_range_minutes: int = 20,
        trade_window_minutes: int = 110,
        bar_range_vol_mult: float = 2.0,
        min_range_width_vol_mult: float = 1.0,
        max_breakout_imbalance: float = 0.20,
        max_breakout_delta_abs_mult: float = 2.0,
        session_close_margin_minutes: int = 5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_minutes  # 1m bars
        self.vol_lookback = vol_lookback
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.min_wait_after_range_ns = min_wait_after_range_minutes * NS_PER_MINUTE
        self.trade_window_ns = trade_window_minutes * NS_PER_MINUTE
        self.bar_range_vol_mult = bar_range_vol_mult
        self.min_range_width_vol_mult = min_range_width_vol_mult
        self.max_breakout_imbalance = max_breakout_imbalance
        self.max_breakout_delta_abs_mult = max_breakout_delta_abs_mult
        self.session_close_margin_ns = session_close_margin_minutes * NS_PER_MINUTE
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.range_ready_ts: int = 0
        self.entry_taken: bool = False

        # Volatility state: current-session true ranges, used only until
        # enough prior-day RTH history exists.
        self.last_close: Optional[float] = None
        self.true_ranges: deque[float] = deque(maxlen=2000)

        # Prior-day RTH true ranges.  This is the buffer that actually matters
        # after the first day or two: it is used to compute a fixed ATR at
        # session start from previous completed regular-hours sessions only.
        self.rth_true_ranges: deque[float] = deque(maxlen=5000)
        self.atr_prior: Optional[float] = None

        # Order-flow state: absolute delta per bar, for the second filter
        self.abs_deltas: deque[float] = deque(maxlen=2000)

        # Trade state
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state for a new session.

        The key part: `atr_prior` is fixed here from the trailing regular-hours
        true ranges of previous sessions.  Current-session bars cannot change
        it.  `last_close` is reset so the first bar of the new session does not
        include the overnight gap, and current-session order-flow history is
        cleared as in #92.
        """
        self.current_session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * NS_PER_HOUR)
        self.range_high = None
        self.range_low = None
        self.range_ready = False
        self.range_ready_ts = 0
        self.entry_taken = False
        self.entry_time = None
        self.entry_order_id = None

        # Determine the volatility scale for the whole session before any
        # current-session RTH bar can contaminate it.
        if len(self.rth_true_ranges) >= self.vol_lookback:
            recent = list(self.rth_true_ranges)[-self.vol_lookback:]
            self.atr_prior = sum(recent) / len(recent)
        else:
            self.atr_prior = None

        self.last_close = None
        self.true_ranges.clear()
        self.abs_deltas.clear()

    def _update_opening_range(self, bar) -> None:
        """Accumulate high/low from bars within the opening range window."""
        if self.range_ready:
            return
        if bar.open_ts < self.rth_open_ts:
            return
        opening_range_end = self.rth_open_ts + self.opening_range_bars * NS_PER_MINUTE
        if bar.open_ts >= opening_range_end:
            self.range_ready = True
            self.range_ready_ts = bar.open_ts
            return
        if self.range_high is None:
            self.range_high = bar.high
            self.range_low = bar.low
        else:
            self.range_high = max(self.range_high, bar.high)
            self.range_low = min(self.range_low, bar.low)

    def _update_atr(self, bar) -> None:
        """Update current-session ATR and, for RTH bars, the prior-day buffer."""
        if self.last_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self.last_close),
                abs(bar.low - self.last_close),
            )
        self.true_ranges.append(tr)
        self.last_close = bar.close

        # Feed the rolling prior-day RTH buffer.  Only regular-hours bars are
        # included; Globex/overnight bars are intentionally excluded.
        if bar.open_ts >= self.rth_open_ts:
            self.rth_true_ranges.append(tr)

    def _atr(self) -> float:
        """Volatility scale used for all thresholds this session.

        Prefer the fixed prior-day RTH ATR.  Fall back to current-session ATR
        only until enough prior-day RTH bars have been collected.
        """
        if self.atr_prior is not None:
            return self.atr_prior
        if len(self.true_ranges) < self.vol_lookback:
            return 0.0
        recent = list(self.true_ranges)[-self.vol_lookback:]
        return sum(recent) / len(recent)

    def _avg_abs_delta(self) -> float:
        """Average absolute signed volume over the current session's lookback."""
        if len(self.abs_deltas) < self.vol_lookback:
            return 0.0
        recent = list(self.abs_deltas)[-self.vol_lookback:]
        return sum(recent) / len(recent)

    def _exit_position(self, ctx) -> None:
        """Cancel bracket legs and flatten the position."""
        if self.entry_order_id is not None:
            legs = ctx.find_orders(parent_id=self.entry_order_id)
            for leg in legs:
                ctx.cancel(leg.id)
        if ctx.position:
            ctx.close()
        self.entry_order_id = None
        self.entry_time = None

    def on_bars(self, ctx) -> None:
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        self._update_atr(bar)
        self.abs_deltas.append(abs(bar.delta))
        self._update_opening_range(bar)

        if not ctx.actionable:
            return

        # Manage existing position
        if ctx.position:
            if self.entry_time is not None and ctx.ts - self.entry_time > self.max_hold_ns:
                self._exit_position(ctx)
                return
            if ctx.session_end - ctx.ts < self.session_close_margin_ns:
                self._exit_position(ctx)
                return
            return

        if ctx.working_order_ids:
            return
        if self.entry_taken:
            return
        if not self.range_ready or self.range_high is None or self.range_low is None:
            return
        if len(self.true_ranges) < self.vol_lookback and self.atr_prior is None:
            return
        if len(self.abs_deltas) < self.vol_lookback:
            return

        time_since_range_ready = bar.close_ts - self.range_ready_ts
        if time_since_range_ready < self.min_wait_after_range_ns:
            return
        if time_since_range_ready > self.trade_window_ns:
            return

        atr = self._atr()
        if atr <= 0:
            return

        range_width = self.range_high - self.range_low
        if range_width < self.min_range_width_vol_mult * atr:
            return

        bar_range = bar.high - bar.low
        if bar_range > self.bar_range_vol_mult * atr:
            return

        threshold = self.breakout_vol_mult * atr
        stop_offset = self.stop_vol_mult * atr
        target_offset = self.target_vol_mult * atr
        volume = max(1, bar.volume)
        avg_abs_delta = self._avg_abs_delta()

        # Fade the breakout: sell on upside breakout, buy on downside breakout.
        # Skip breakouts whose signed volume is strongly aligned with the move,
        # either as a fraction of the breakout bar's own volume or as an
        # unusually large absolute flow relative to recent bars.
        if bar.close > self.range_high + threshold:
            if bar.delta > self.max_breakout_imbalance * volume:
                return
            if (
                avg_abs_delta > 0
                and bar.delta > self.max_breakout_delta_abs_mult * avg_abs_delta
            ):
                return
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade_short",
            )
            if order_id is not None:
                self.entry_taken = True
                self.entry_order_id = order_id
        elif bar.close < self.range_low - threshold:
            if bar.delta < -self.max_breakout_imbalance * volume:
                return
            if (
                avg_abs_delta > 0
                and bar.delta < -self.max_breakout_delta_abs_mult * avg_abs_delta
            ):
                return
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="fade_long",
            )
            if order_id is not None:
                self.entry_taken = True
                self.entry_order_id = order_id

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position != 0 and self.entry_time is None:
            self.entry_time = ctx.ts
        elif fill.position == 0:
            self.entry_time = None
            self.entry_order_id = None

    def on_lifecycle(self, ctx) -> None:
        event = ctx.event
        if event.order_id == self.entry_order_id and event.kind in (
            "day_expired",
            "cancel_confirmed",
            "order_rejected",
        ):
            self.entry_order_id = None
            self.entry_time = None
