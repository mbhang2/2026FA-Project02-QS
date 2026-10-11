"""Opening range breakout strategy.

Investigates whether a move out of the opening range holds or fades.
Defines the opening range as the first 30 minutes of the regular-hours session
(9:30–10:00 ET). Uses a volatility-scaled threshold to identify breakouts.
Trades breakouts in the direction of the move with a protective bracket.
Closes positions near session end to avoid overnight exposure.
"""

from __future__ import annotations

import statistics
from collections import deque
from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Breakout of the opening range, volatility-scaled."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_range_minutes: int = 30,
        breakout_vol_mult: float = 0.5,
        stop_vol_mult: float = 1.0,
        target_vol_mult: float = 2.0,
        min_vol_samples: int = 100,
        max_hold_bars: int = 60,
        session_close_margin_seconds: int = 300,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_range_minutes = opening_range_minutes
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.min_vol_samples = min_vol_samples
        self.max_hold_bars = max_hold_bars
        self.session_close_margin_seconds = session_close_margin_seconds
        self.size = size

        # State
        self.session: Optional[int] = None
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.entry_bar_count: int = 0
        self.last_close: Optional[float] = None
        self.returns: deque[float] = deque(maxlen=2000)  # trailing 1m returns

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Only act on our interval
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Session change detection
        if ctx.session != self.session:
            self.session = ctx.session
            self.range_high = None
            self.range_low = None
            self.range_ready = False
            self.entry_bar_count = 0
            self.last_close = None

        # Update returns for volatility
        if self.last_close is not None and self.last_close != 0:
            ret = (bar.close - self.last_close) / self.last_close
            self.returns.append(ret)
        self.last_close = bar.close

        # Compute regular-hours open timestamp (17:00 CT + 15.5h = 8:30 CT)
        rth_open_ts = ctx.session_start + 55800 * NS_PER_SECOND
        opening_range_end_ts = rth_open_ts + self.opening_range_minutes * 60 * NS_PER_SECOND

        # Build opening range
        if not self.range_ready:
            if bar.open_ts >= rth_open_ts and bar.open_ts < opening_range_end_ts:
                if self.range_high is None or bar.high > self.range_high:
                    self.range_high = bar.high
                if self.range_low is None or bar.low < self.range_low:
                    self.range_low = bar.low
            elif bar.open_ts >= opening_range_end_ts:
                # First bar after the opening range period
                self.range_ready = True

        # Trading logic
        if not ctx.actionable:
            return

        # Time-based exit: close near session end
        if ctx.position and (ctx.session_end - ctx.ts) < self.session_close_margin_seconds * NS_PER_SECOND:
            # Cancel any working orders (including bracket legs) before closing
            ctx.cancel_all()
            ctx.close()
            return

        # Max hold exit
        if ctx.position:
            self.entry_bar_count += 1
            if self.entry_bar_count >= self.max_hold_bars:
                ctx.cancel_all()
                ctx.close()
                return
            return  # already in position, let bracket manage

        # Entry logic
        if not self.range_ready or self.range_high is None or self.range_low is None:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # Compute volatility threshold
        if len(self.returns) < self.min_vol_samples:
            return
        vol = statistics.stdev(self.returns)  # sample std of returns
        if vol <= 0:
            return
        threshold = self.breakout_vol_mult * vol * bar.close

        # Breakout detection
        if bar.close > self.range_high + threshold:
            stop_offset = self.stop_vol_mult * vol * bar.close
            target_offset = self.target_vol_mult * vol * bar.close
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="breakout_long",
            )
            self.entry_bar_count = 0
        elif bar.close < self.range_low - threshold:
            stop_offset = self.stop_vol_mult * vol * bar.close
            target_offset = self.target_vol_mult * vol * bar.close
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="breakout_short",
            )
            self.entry_bar_count = 0

    def on_fill(self, ctx) -> None:
        # Reset entry bar count on fill
        if ctx.fill.position != 0:
            self.entry_bar_count = 0

    def on_lifecycle(self, ctx) -> None:
        pass
