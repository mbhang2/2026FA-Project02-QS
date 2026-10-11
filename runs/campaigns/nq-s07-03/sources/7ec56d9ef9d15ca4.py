"""Opening range fade strategy.

Investigates whether price leaving the opening range tends to revert (range wins)
rather than continue (move holds). The opening range is defined as the high/low
of the first 30 minutes (6 five-minute bars) of the regular trading session
(8:30–15:15 CT). A breakout is defined as price moving beyond the range by a
buffer proportional to the average daily range of the last 20 sessions. When a
breakout occurs, we fade it: sell on upside breakout, buy on downside breakout.
Positions are protected by a bracket (stop and target) and are also closed after
a fixed number of bars (time exit) or shortly before the regular session close.
The strategy only acts during the regular session, never overnight.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        daily_interval: str = "1d",
        opening_range_bars: int = 6,          # 30 minutes at 5m
        volatility_lookback: int = 20,        # sessions for avg daily range
        breakout_buffer_mult: float = 0.5,    # buffer as fraction of avg range
        stop_mult: float = 1.0,               # stop distance as fraction of avg range
        target_mult: float = 1.5,             # target distance as fraction of avg range
        max_hold_bars: int = 12,              # 1 hour at 5m
        close_margin_minutes: int = 5,        # close before regular close
        size: int = 1,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_range_bars = opening_range_bars
        self.volatility_lookback = volatility_lookback
        self.breakout_buffer_mult = breakout_buffer_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_hold_bars = max_hold_bars
        self.close_margin_minutes = close_margin_minutes
        self.size = size

        # State
        self.current_session: int | None = None
        self.regular_open_ts: int | None = None
        self.regular_close_ts: int | None = None
        self.opening_bars: list[Any] = []
        self.opening_high: float | None = None
        self.opening_low: float | None = None
        self.opening_ready: bool = False
        self.daily_ranges: deque[float] = deque(maxlen=self.volatility_lookback)
        self.avg_range: float | None = None
        self.position_entry_bar_count: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def _compute_regular_times(self, ctx) -> None:
        """Compute regular session open/close timestamps for the current session."""
        ct = ZoneInfo("America/Chicago")
        session_start_dt = datetime.fromtimestamp(ctx.session_start / 1e9, tz=ct)
        # Regular open is 8:30 CT on the day after the Globex open (17:00 CT)
        regular_open_date = session_start_dt.date() + timedelta(days=1)
        regular_open_dt = datetime.combine(regular_open_date, time(8, 30), tzinfo=ct)
        self.regular_open_ts = int(regular_open_dt.timestamp() * 1e9)
        # Regular close is 45 minutes before session end (session end is 16:00 CT)
        self.regular_close_ts = ctx.session_end - 45 * 60 * 1_000_000_000

    def _update_volatility(self, bar) -> None:
        """Update rolling average daily range from a completed daily bar."""
        rng = bar.high - bar.low
        if rng > 0:
            self.daily_ranges.append(rng)
            if len(self.daily_ranges) >= 2:  # need at least a couple for stability
                self.avg_range = sum(self.daily_ranges) / len(self.daily_ranges)

    def on_bars(self, ctx) -> None:
        # Update volatility when a daily bar closes (session end)
        if self.daily_interval in ctx.bars:
            daily_bar = ctx.bars[self.daily_interval]
            self._update_volatility(daily_bar)

        # Only act on our primary interval
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Detect new session
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.opening_bars = []
            self.opening_high = None
            self.opening_low = None
            self.opening_ready = False
            self.position_entry_bar_count = 0
            self._compute_regular_times(ctx)

        # If not actionable, do nothing
        if not ctx.actionable:
            return

        # Ensure regular times are set (should be after session detection)
        if self.regular_open_ts is None or self.regular_close_ts is None:
            return

        # Check if we are inside the regular session
        if not (self.regular_open_ts <= ctx.ts < self.regular_close_ts):
            return

        # Manage open position: time exit and close before session end
        if ctx.position != 0:
            self.position_entry_bar_count += 1
            # Time exit
            if self.position_entry_bar_count >= self.max_hold_bars:
                ctx.close()
                return
            # Close before regular close
            if ctx.ts >= self.regular_close_ts - self.close_margin_minutes * 60 * 1_000_000_000:
                ctx.close()
                return
            # If we have a position, we don't look for new entries
            return

        # If we have working orders (e.g., bracket legs), don't enter again
        if ctx.working_order_ids:
            return

        # Collect opening range bars
        if not self.opening_ready:
            if bar.open_ts >= self.regular_open_ts and len(self.opening_bars) < self.opening_range_bars:
                self.opening_bars.append(bar)
                if len(self.opening_bars) == self.opening_range_bars:
                    self.opening_high = max(b.high for b in self.opening_bars)
                    self.opening_low = min(b.low for b in self.opening_bars)
                    self.opening_ready = True
            return

        # Need volatility estimate
        if self.avg_range is None or self.avg_range <= 0:
            return

        # Check if enough time remains in regular session (at least 30 minutes)
        if self.regular_close_ts - ctx.ts < 30 * 60 * 1_000_000_000:
            return

        # Breakout buffer
        buffer = self.breakout_buffer_mult * self.avg_range

        # Current price (use bar close)
        price = bar.close

        # Fade logic: if price breaks above range, sell; if below, buy
        if self.opening_high is not None and price > self.opening_high + buffer:
            # Upside breakout -> fade by selling
            stop_dist = self.stop_mult * self.avg_range
            target_dist = self.target_mult * self.avg_range
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="fade-short",
            )
            self.position_entry_bar_count = 0
        elif self.opening_low is not None and price < self.opening_low - buffer:
            # Downside breakout -> fade by buying
            stop_dist = self.stop_mult * self.avg_range
            target_dist = self.target_mult * self.avg_range
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="fade-long",
            )
            self.position_entry_bar_count = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
