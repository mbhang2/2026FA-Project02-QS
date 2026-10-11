"""Opening range fade with robust volatility, low-volume breakout filter, and later entry window.

Investigates whether price leaving the opening range tends to revert (range wins).
The opening range is the first 30 minutes of the regular trading hours (RTH)
session, defined as the high and low of the first 30 one-minute bars after the
RTH open (15.5 hours after the Globex session start). Volatility is measured by
the median absolute deviation (MAD) of 1-minute returns over the last 100 bars,
scaled by 1.4826 to be comparable to standard deviation but robust to outliers.
A breakout is defined as a 1-minute bar closing beyond the range by 0.8 times
this robust volatility. To avoid the very first bars after the range is
established, entries are only taken at least 45 minutes after the range is ready
(previous best used 15 minutes). The trade window is 120 minutes, so entries
occur between 45 and 165 minutes after the range is ready. This shifts the entry
window later in the session, targeting the late-morning hours that showed the
most consistent fade profitability in prior concentration reports. Additionally,
we require the breakout bar's high-low range to be less than 3 times the robust
volatility, filtering out extremely volatile bars that are more likely to be
news-driven and less likely to revert. A new filter is added: the breakout bar's
volume must be less than 0.8 times the average volume of the last 20 bars. This
selects low-volume breakouts, which are more likely to be weak moves that revert
rather than strong directional moves that continue. We fade the breakout: sell
on an upside breakout, buy on a downside breakout. Only the first breakout of
each session is traded. The position is protected by a bracket with stop and
target offsets of 0.8 and 2.5 times the robust volatility, respectively. If
neither stop nor target is hit within 120 minutes, the position is closed at
market. Positions are also flattened 5 minutes before session end. This design
aims to trade most sessions while improving entry quality and robustness
compared to the prior best strategy (#30).
"""

from __future__ import annotations

import statistics
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
        vol_lookback: int = 100,
        breakout_vol_mult: float = 0.8,
        stop_vol_mult: float = 0.8,
        target_vol_mult: float = 2.5,
        max_hold_minutes: int = 120,
        min_wait_after_range_minutes: int = 45,
        trade_window_minutes: int = 120,
        bar_range_vol_mult: float = 3.0,
        volume_lookback: int = 20,
        volume_filter_mult: float = 0.8,
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
        self.volume_lookback = volume_lookback
        self.volume_filter_mult = volume_filter_mult
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

        # Volatility state
        self.last_close: Optional[float] = None
        self.returns: deque[float] = deque(maxlen=2000)

        # Volume state
        self.volumes: deque[int] = deque(maxlen=volume_lookback)

        # Trade state
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state for a new session."""
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
        # Reset last_close so the first bar of a new session does not compute
        # a return from the previous session's close (avoids overnight gap).
        self.last_close = None
        # Reset volume history for the new session
        self.volumes.clear()

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

    def _robust_volatility(self) -> float:
        """Compute robust volatility as 1.4826 * median absolute deviation."""
        if len(self.returns) < self.vol_lookback:
            return 0.0
        recent = list(self.returns)[-self.vol_lookback:]
        med = statistics.median(recent)
        abs_dev = [abs(r - med) for r in recent]
        mad = statistics.median(abs_dev)
        return 1.4826 * mad

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

        # Update volatility (skip first bar of session to avoid overnight gap)
        if self.last_close is not None and self.last_close != 0:
            ret = (bar.close - self.last_close) / self.last_close
            self.returns.append(ret)
        self.last_close = bar.close

        # Update volume history (always, even during warmup)
        self.volumes.append(bar.volume)

        # Update opening range
        self._update_opening_range(bar)

        # Trading logic only when actionable
        if not ctx.actionable:
            return

        # Manage existing position
        if ctx.position:
            # Time-based exit
            if self.entry_time is not None and ctx.ts - self.entry_time > self.max_hold_ns:
                self._exit_position(ctx)
                return
            # Session-end exit
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
        if len(self.returns) < self.vol_lookback:
            return

        # Time filter: only trade after min_wait_after_range and within trade_window
        time_since_range_ready = bar.close_ts - self.range_ready_ts
        if time_since_range_ready < self.min_wait_after_range_ns:
            return
        if time_since_range_ready > self.trade_window_ns:
            return

        vol = self._robust_volatility()
        if vol <= 0:
            return

        # Bar range filter: skip extremely volatile bars
        bar_range = bar.high - bar.low
        if bar_range > self.bar_range_vol_mult * vol * bar.close:
            return

        # Volume filter: require low volume relative to recent average
        if len(self.volumes) < self.volume_lookback:
            return
        avg_volume = sum(self.volumes) / len(self.volumes)
        if bar.volume > self.volume_filter_mult * avg_volume:
            return

        threshold = self.breakout_vol_mult * vol * bar.close
        stop_offset = self.stop_vol_mult * vol * bar.close
        target_offset = self.target_vol_mult * vol * bar.close

        # Fade the breakout: sell on upside breakout, buy on downside breakout
        if bar.close > self.range_high + threshold:
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
