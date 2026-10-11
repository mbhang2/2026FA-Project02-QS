"""Opening range breakout, time-filtered and one trade per session.

Investigates whether a move out of the opening range holds when it occurs after
the initial post-open noise has subsided. The opening range is the first 30
minutes of the regular trading hours (RTH) session. A breakout is defined as a
1-minute bar closing beyond the range by a volatility-scaled threshold, where
volatility is the standard deviation of trailing 1-minute returns. To avoid the
losing early hours identified in prior work, we only consider breakouts that
occur between 60 and 240 minutes after the RTH open. We take at most one trade
per session, entering in the direction of the breakout with a protective
bracket (stop and target offsets also scaled by volatility). Positions are
closed after a maximum holding period or near the session end.
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
        min_wait_minutes: int = 60,
        max_wait_minutes: int = 240,
        breakout_vol_mult: float = 0.5,
        stop_vol_mult: float = 1.0,
        target_vol_mult: float = 2.0,
        min_vol_samples: int = 100,
        max_hold_bars: int = 60,
        session_close_margin_seconds: int = 300,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_minutes = opening_range_minutes
        self.min_wait_ns = min_wait_minutes * NS_PER_MINUTE
        self.max_wait_ns = max_wait_minutes * NS_PER_MINUTE
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.min_vol_samples = min_vol_samples
        self.max_hold_bars = max_hold_bars
        self.session_close_margin_ns = session_close_margin_seconds * NS_PER_SECOND
        self.size = size

        # Per-session state
        self.session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.traded_today: bool = False
        self.entry_bar_count: int = 0
        self.last_close: Optional[float] = None
        self.returns: deque[float] = deque(maxlen=2000)  # trailing 1m returns

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state and compute RTH open timestamp."""
        self.session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * NS_PER_HOUR)
        self.range_high = None
        self.range_low = None
        self.range_ready = False
        self.traded_today = False
        self.entry_bar_count = 0
        self.last_close = None

    def _update_opening_range(self, bar) -> None:
        """Accumulate high/low from bars within the opening range window."""
        if self.range_ready or self.rth_open_ts == 0:
            return
        if bar.open_ts < self.rth_open_ts:
            return
        if bar.open_ts >= self.rth_open_ts + self.opening_range_minutes * NS_PER_MINUTE:
            self.range_ready = True
            return
        if self.range_high is None:
            self.range_high = bar.high
            self.range_low = bar.low
        else:
            self.range_high = max(self.range_high, bar.high)
            self.range_low = min(self.range_low, bar.low)

    def _exit_position(self, ctx) -> None:
        """Cancel all working orders and flatten the position."""
        ctx.cancel_all()
        ctx.close()
        self.entry_bar_count = 0

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.session:
            self._reset_session(ctx)

        # Only process our interval
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Update returns for volatility
        if self.last_close is not None and self.last_close != 0:
            ret = (bar.close - self.last_close) / self.last_close
            self.returns.append(ret)
        self.last_close = bar.close

        # Build opening range
        self._update_opening_range(bar)

        # Trading logic only when actionable
        if not ctx.actionable:
            return

        # Time-based exit: close near session end
        if ctx.position and (ctx.session_end - ctx.ts) < self.session_close_margin_ns:
            self._exit_position(ctx)
            return

        # Max hold exit
        if ctx.position:
            self.entry_bar_count += 1
            if self.entry_bar_count >= self.max_hold_bars:
                self._exit_position(ctx)
                return
            return  # already in position, let bracket manage

        # Entry logic
        if not self.range_ready or self.range_high is None or self.range_low is None:
            return
        if self.traded_today or ctx.position or ctx.working_order_ids:
            return

        # Time filter: only trade within the specified window after RTH open
        time_since_open = ctx.ts - self.rth_open_ts
        if time_since_open < self.min_wait_ns or time_since_open > self.max_wait_ns:
            return

        # Compute volatility threshold
        if len(self.returns) < self.min_vol_samples:
            return
        vol = statistics.stdev(self.returns)
        if vol <= 0:
            return
        threshold = self.breakout_vol_mult * vol * bar.close

        # Breakout detection
        if bar.close > self.range_high + threshold:
            stop_offset = self.stop_vol_mult * vol * bar.close
            target_offset = self.target_vol_mult * vol * bar.close
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="orb_breakout_long",
            )
            if order_id is not None:
                self.traded_today = True
                self.entry_bar_count = 0
        elif bar.close < self.range_low - threshold:
            stop_offset = self.stop_vol_mult * vol * bar.close
            target_offset = self.target_vol_mult * vol * bar.close
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="orb_breakout_short",
            )
            if order_id is not None:
                self.traded_today = True
                self.entry_bar_count = 0

    def on_fill(self, ctx) -> None:
        # Reset entry bar count on fill
        if ctx.fill.position != 0:
            self.entry_bar_count = 0

    def on_lifecycle(self, ctx) -> None:
        pass
