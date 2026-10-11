"""Opening range fade with volatility scaled by the prior regular-hours session.

This is the #92 skeleton with one deliberate change aimed directly at the
campaign's volatility definition.  #92 measures the breakout threshold, range
filters, and bracket distances with an ATR buffer that is filled from the
current Globex session and the early part of the current RTH session.  That is
today's volatility, not the "trailing realized volatility computed from prior
days" the campaign specifies.

Here we accumulate one-minute true ranges for each regular-hours session
separately.  At the start of a new session we compute the average true range of
the previous RTH session (last `vol_lookback` one-minute true ranges) and use
that prior-day RTH ATR for the *signal* scale: the breakout threshold, the
minimum opening-range width, and the maximum breakout-bar range.

Stop and target distances remain scaled by the current session's ATR when it is
available, because they represent the risk of the position we are about to open
today.  If the current ATR has not yet accumulated enough bars, we fall back to
the prior RTH ATR.  If there is no prior RTH ATR (first session after warmup),
the signal scale falls back to the current ATR, exactly as #92 did.

Everything else is unchanged from #92: the 30-minute RTH opening range, the 1m
bar close outside the range by `breakout_vol_mult`, the same-session
order-flow filters (within-bar imbalance and absolute delta), the
first-qualifying-breakout rule, the 20-110 minute entry window after the range
is ready, the 100-minute maximum hold, and the 5-minute session-end margin.
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
        use_prior_rth_atr: bool = True,
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
        self.use_prior_rth_atr = use_prior_rth_atr

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.range_ready_ts: int = 0
        self.entry_taken: bool = False

        # Volatility state
        self.last_close: Optional[float] = None
        self.true_ranges: deque[float] = deque(maxlen=2000)

        # RTH-only true ranges for the current session.  These are used to
        # compute the next session's prior-day RTH ATR.
        self.rth_true_ranges: deque[float] = deque(maxlen=2000)
        self.prior_rth_atr: Optional[float] = None

        # Order-flow state: absolute delta per bar, for the second filter
        self.abs_deltas: deque[float] = deque(maxlen=2000)

        # Trade state
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state and compute the previous RTH session's ATR."""
        # The rth_true_ranges deque contains only bars from the session that
        # just ended, because it is cleared here after this computation and
        # appended to only when bar.open_ts >= rth_open_ts.
        if len(self.rth_true_ranges) >= self.vol_lookback:
            recent = list(self.rth_true_ranges)[-self.vol_lookback:]
            self.prior_rth_atr = sum(recent) / len(recent)
        else:
            self.prior_rth_atr = None

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

        # Same-session ATR is reset, as in #92.  RTH-only true ranges are
        # cleared now that the prior session's ATR has been computed.
        self.last_close = None
        self.true_ranges.clear()
        self.rth_true_ranges.clear()
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
        """Update the current-session ATR and the RTH-only true-range buffer."""
        if self.last_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self.last_close),
                abs(bar.low - self.last_close),
            )
        self.true_ranges.append(tr)
        if self.rth_open_ts and bar.open_ts >= self.rth_open_ts:
            self.rth_true_ranges.append(tr)
        self.last_close = bar.close

    def _atr(self) -> float:
        """Average true range over the current-session lookback window."""
        if len(self.true_ranges) < self.vol_lookback:
            return 0.0
        recent = list(self.true_ranges)[-self.vol_lookback:]
        return sum(recent) / len(recent)

    def _signal_atr(self) -> float:
        """Volatility scale for breakout detection and range filters.

        Prefer the previous RTH session's average true range, as measured
        before today's RTH session begins.  This is the trailing realized
        volatility from prior days that the campaign asks for.
        """
        if self.use_prior_rth_atr and self.prior_rth_atr is not None:
            return self.prior_rth_atr
        return self._atr()

    def _risk_atr(self) -> float:
        """Volatility scale for the protective bracket distances."""
        atr = self._atr()
        if atr > 0.0:
            return atr
        sig_atr = self._signal_atr()
        return sig_atr if sig_atr > 0.0 else 0.0

    def _avg_abs_delta(self) -> float:
        """Average absolute signed volume over the lookback window."""
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
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        # Only process our interval
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Update ATR and order-flow history (always, even during warmup)
        self._update_atr(bar)
        self.abs_deltas.append(abs(bar.delta))

        # Update opening range
        self._update_opening_range(bar)

        # Trading logic only when actionable
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
            return  # let bracket manage

        # No position, no working orders: consider new entry
        if ctx.working_order_ids:
            return
        if self.entry_taken:
            return
        if not self.range_ready or self.range_high is None or self.range_low is None:
            return
        if len(self.abs_deltas) < self.vol_lookback:
            return

        # Time filter: only trade after min_wait_after_range and within trade_window
        time_since_range_ready = bar.close_ts - self.range_ready_ts
        if time_since_range_ready < self.min_wait_after_range_ns:
            return
        if time_since_range_ready > self.trade_window_ns:
            return

        signal_atr = self._signal_atr()
        risk_atr = self._risk_atr()
        if signal_atr <= 0.0 or risk_atr <= 0.0:
            return

        # Opening range width filter: require range to be at least min_range_width_vol_mult * signal ATR
        range_width = self.range_high - self.range_low
        if range_width < self.min_range_width_vol_mult * signal_atr:
            return

        # Bar range filter: skip extremely volatile bars
        bar_range = bar.high - bar.low
        if bar_range > self.bar_range_vol_mult * signal_atr:
            return

        threshold = self.breakout_vol_mult * signal_atr
        stop_offset = self.stop_vol_mult * risk_atr
        target_offset = self.target_vol_mult * risk_atr
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
                avg_abs_delta > 0.0
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
                avg_abs_delta > 0.0
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
            # Entry fill
            self.entry_time = ctx.ts
        elif fill.position == 0:
            # Position closed (stop, target, or manual exit)
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
