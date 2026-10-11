"""Opening range breakout, filtered by time of day and limited to one trade per session.

Investigates whether a move out of the opening range holds when it occurs after
the initial post-open noise has subsided. The opening range is the first 30
minutes of the regular trading hours (RTH) session. A breakout is defined as a
1-minute bar closing beyond the range by a volatility-scaled threshold, where
volatility is the average true range (ATR) of the previous 20 daily bars. To
avoid the losing early hours identified in prior work, we only consider
breakouts that occur at least 60 minutes after the RTH open. We take at most
one trade per session, entering in the direction of the breakout with a
protective bracket (stop and target offsets also scaled by ATR). Positions are
closed after a maximum holding period or near the session end.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND

NS_PER_MINUTE = 60 * NS_PER_SECOND
NS_PER_HOUR = 3600 * NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        daily_interval: str = "1d",
        opening_range_minutes: int = 30,
        atr_lookback: int = 20,
        breakout_mult: float = 0.5,
        stop_mult: float = 1.0,
        target_mult: float = 2.0,
        trade_after_minutes: int = 60,
        max_hold_minutes: int = 120,
        session_close_margin_minutes: int = 5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_range_minutes = opening_range_minutes
        self.atr_lookback = atr_lookback
        self.breakout_mult = breakout_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.trade_after_minutes = trade_after_minutes
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.session_close_margin_ns = session_close_margin_minutes * NS_PER_MINUTE
        self.size = size

        # Per-session state
        self.session: Optional[int] = None
        self.rth_open_ts: int = 0
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_ready: bool = False
        self.traded_today: bool = False
        self.entry_order_id: Optional[int] = None
        self.entry_time: int = 0
        self.atr: Optional[float] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state and compute ATR from previous daily bars."""
        self.session = ctx.session
        # RTH open is 15.5 hours after Globex session start (17:00 CT -> 8:30 CT)
        self.rth_open_ts = ctx.session_start + int(15.5 * NS_PER_HOUR)
        self.range_high = None
        self.range_low = None
        self.range_ready = False
        self.traded_today = False
        self.entry_order_id = None
        self.entry_time = 0

        # Compute ATR from the last `atr_lookback` daily bars (previous sessions)
        daily = ctx.history(self.daily_interval)
        if len(daily) >= self.atr_lookback:
            recent = daily[-self.atr_lookback:]
            ranges = [bar.high - bar.low for bar in recent]
            self.atr = sum(ranges) / len(ranges)
        else:
            self.atr = None

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
        """Cancel bracket legs and flatten the position."""
        if self.entry_order_id is not None:
            legs = ctx.find_orders(parent_id=self.entry_order_id)
            for leg in legs:
                ctx.cancel(leg.id)
        ctx.close()
        self.entry_order_id = None
        self.entry_time = 0

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.session:
            self._reset_session(ctx)

        # Process 1-minute bars
        if self.interval in ctx.bars:
            bar = ctx.bars[self.interval]
            self._update_opening_range(bar)

            # Check for breakout if range is ready, we haven't traded, and we are flat
            if (
                self.range_ready
                and not self.traded_today
                and self.atr is not None
                and self.atr > 0
                and self.range_high is not None
                and self.range_low is not None
                and not ctx.position
                and not ctx.working_order_ids
            ):
                # Only trade after the specified time after RTH open
                if ctx.ts >= self.rth_open_ts + self.trade_after_minutes * NS_PER_MINUTE:
                    threshold = self.breakout_mult * self.atr
                    if bar.close > self.range_high + threshold:
                        self.entry_order_id = ctx.buy(
                            size=self.size,
                            stop_offset=self.stop_mult * self.atr,
                            target_offset=self.target_mult * self.atr,
                            tag="orb_breakout_long",
                        )
                        if self.entry_order_id is not None:
                            self.traded_today = True
                    elif bar.close < self.range_low - threshold:
                        self.entry_order_id = ctx.sell(
                            size=self.size,
                            stop_offset=self.stop_mult * self.atr,
                            target_offset=self.target_mult * self.atr,
                            tag="orb_breakout_short",
                        )
                        if self.entry_order_id is not None:
                            self.traded_today = True

        # Time-based exit: max holding period
        if (
            ctx.position
            and self.entry_time > 0
            and ctx.ts - self.entry_time > self.max_hold_ns
        ):
            self._exit_position(ctx)

        # Session-end exit
        if (
            ctx.position
            and ctx.session_end - ctx.ts < self.session_close_margin_ns
        ):
            self._exit_position(ctx)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position != 0 and self.entry_time == 0:
            # This fill opened a position (entry fill)
            self.entry_time = ctx.ts
        elif fill.position == 0:
            # Position closed (stop, target, or manual exit)
            self.entry_time = 0
            self.entry_order_id = None

    def on_lifecycle(self, ctx) -> None:
        event = ctx.event
        if event.order_id == self.entry_order_id and event.kind in (
            "day_expired",
            "cancel_confirmed",
            "order_rejected",
        ):
            self.entry_order_id = None
            self.entry_time = 0
