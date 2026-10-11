"""Opening range fade with dynamic target, range-width filter, and trend filter.

Investigates whether price leaving the opening range tends to revert (range wins).
The opening range is the first 30 minutes of the regular trading hours (RTH)
session, defined as the high and low of the first 30 one-minute bars after the
RTH open (15.5 hours after the Globex session start). Volatility is measured by
the median absolute deviation (MAD) of 1-minute returns over the last 100 bars,
scaled by 1.4826 to be comparable to standard deviation but robust to outliers.
A breakout is defined as a 1-minute bar closing beyond the range by 0.6 times
this robust volatility. To avoid the very first bars after the range is
established, entries are only taken at least 30 minutes after the range is ready.
The trade window extends for 180 minutes after that delay. Additionally, we
require the breakout bar's high-low range to be less than 3 times the robust
volatility, and the breakout distance beyond the range to be at least 25% of the
range width. We also filter by the opening range's internal trend: if the range
sloped upward, we only fade downside breakouts; if it sloped downward, we only
fade upside breakouts. We fade the breakout: sell on an upside breakout, buy on a
downside breakout. Only the first breakout of each session is traded. The
position is protected by a bracket with a stop offset of 0.8 times volatility and
a target offset that is the maximum of the distance to the opposite range
boundary and 1.0 times volatility. If neither stop nor target is hit within 90
minutes, the position is closed at market. Positions are also flattened 5 minutes
before session end. This design aims to trade most sessions while improving entry
quality and risk/reward compared to the prior best strategy (#19).
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
        breakout_vol_mult: float = 0.6,
        stop_vol_mult: float = 0.8,
        min_target_vol_mult: float = 1.0,
        max_hold_minutes: int = 90,
        min_wait_after_range_minutes: int = 30,
        trade_window_minutes: int = 180,
        bar_range_vol_mult: float = 3.0,
        range_width_frac: float = 0.25,
        session_close_margin_minutes: int = 5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_minutes  # 1m bars
        self.vol_lookback = vol_lookback
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.min_target_vol_mult = min_target_vol_mult
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.min_wait_after_range_ns = min_wait_after_range_minutes * NS_PER_MINUTE
        self.trade_window_ns = trade_window_minutes * NS_PER_MINUTE
        self.bar_range_vol_mult = bar_range_vol_mult
        self.range_width_frac = range_width_frac
        self.session_close_margin_ns = session_close_margin_minutes * NS_PER_MINUTE
        self.size = size

        # Session state
        self.current_session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_first_close: Optional[float] = None
        self.range_last_close: Optional[float] = None
        self.range_ready: bool = False
        self.range_ready_ts: int = 0
        self.entry_taken: bool = False

        # Volatility state
        self.last_close: Optional[float] = None
        self.returns: deque[float] = deque(maxlen=2000)

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
        self.range_first_close = None
        self.range_last_close = None
        self.range_ready = False
        self.range_ready_ts = 0
        self.entry_taken = False
        self.entry_time = None
        self.entry_order_id = None
        # Reset last_close so the first bar of a new session does not compute
        # a return from the previous session's close (avoids overnight gap).
        self.last_close = None

    def _update_opening_range(self, bar) -> None:
        """Accumulate high/low and first/last close from bars within the opening range window."""
        if self.range_ready:
            return
        if bar.open_ts < self.rth_open_ts:
            return
        opening_range_end = self.rth_open_ts + self.opening_range_bars * NS_PER_MINUTE
        if bar.open_ts >= opening_range_end:
            self.range_ready = True
            self.range_ready_ts = bar.open_ts
            # The last bar of the range is the one that just closed before this check.
            # We need to capture its close as range_last_close.
            # Since we are called on bar close, the bar that triggered this is the first bar after the range.
            # The previous bar was the last bar of the range. We stored its close in self.last_close.
            # But we need to be careful: self.last_close is updated after this method in on_bars.
            # So we can use the previous bar's close from history if needed.
            # Simpler: we can store the close of the last bar that was within the range.
            # We'll handle it in on_bars by checking if range_ready just became true.
            return
        if self.range_high is None:
            self.range_high = bar.high
            self.range_low = bar.low
            self.range_first_close = bar.close
        else:
            self.range_high = max(self.range_high, bar.high)
            self.range_low = min(self.range_low, bar.low)
        # Update last close of range (will be overwritten each bar until range ends)
        self.range_last_close = bar.close

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

        # Update opening range
        was_range_ready = self.range_ready
        self._update_opening_range(bar)
        if not was_range_ready and self.range_ready:
            # Range just became ready. The last bar of the range was the previous bar.
            # We can get its close from history if available.
            history = ctx.history(self.interval)
            if len(history) >= 2:
                self.range_last_close = history[-2].close
            else:
                # Fallback: use current bar's open as approximation
                self.range_last_close = bar.open

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

        # Range width filter: require breakout distance beyond range to be at least
        # range_width_frac * range_width
        range_width = self.range_high - self.range_low
        if range_width <= 0:
            return
        min_breakout_dist = self.range_width_frac * range_width

        threshold = self.breakout_vol_mult * vol * bar.close
        stop_offset = self.stop_vol_mult * vol * bar.close

        # Determine range trend
        if self.range_first_close is not None and self.range_last_close is not None:
            range_slope = self.range_last_close - self.range_first_close
        else:
            range_slope = 0.0

        # Fade the breakout: sell on upside breakout, buy on downside breakout
        # Apply trend filter: if range sloped up, only fade downside breakouts (buy);
        # if sloped down, only fade upside breakouts (sell); if flat, trade both.
        if bar.close > self.range_high + threshold:
            # Upside breakout -> sell (fade)
            if range_slope >= 0:
                # Range sloped up or flat, skip upside fade
                return
            breakout_dist = bar.close - self.range_high
            if breakout_dist < min_breakout_dist:
                return
            # Dynamic target: distance to range_high (reversion point) or min_target_vol_mult * vol * price
            target_offset = max(self.range_high - bar.close, self.min_target_vol_mult * vol * bar.close)
            # Ensure target_offset is positive
            if target_offset <= 0:
                target_offset = self.min_target_vol_mult * vol * bar.close
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
            # Downside breakout -> buy (fade)
            if range_slope <= 0:
                # Range sloped down or flat, skip downside fade
                return
            breakout_dist = self.range_low - bar.close
            if breakout_dist < min_breakout_dist:
                return
            # Dynamic target: distance to range_low (reversion point) or min_target_vol_mult * vol * price
            target_offset = max(bar.close - self.range_low, self.min_target_vol_mult * vol * bar.close)
            if target_offset <= 0:
                target_offset = self.min_target_vol_mult * vol * bar.close
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
