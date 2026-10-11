"""Opening range breakout with volatility-scaled threshold and time-based exit.

The strategy defines the opening range as the high and low of the first 30
minutes of the regular trading hours (RTH) session. RTH open is computed as
session start + 15.5 hours (55,800,000,000,000 ns), which is constant across
DST because both times are in CT. Volatility is the average true range (high -
low) of the last 20 completed daily bars. A breakout occurs when a 1-minute bar
closes beyond the range by 0.5 * ATR. Entry is a market order with a bracket:
stop at 1.0 * ATR, target at 2.0 * ATR. If neither is hit within 2 hours, the
position is closed at market. Positions are also flattened 5 minutes before
session end. The strategy trades every session where the opening range is
established and a breakout occurs.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        range_minutes: int = 30,
        atr_lookback: int = 20,
        breakout_mult: float = 0.5,
        stop_mult: float = 1.0,
        target_mult: float = 2.0,
        horizon_minutes: int = 120,
        exit_margin_minutes: int = 5,
        size: int = 1,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.atr_lookback = atr_lookback
        self.breakout_mult = breakout_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.horizon_minutes = horizon_minutes
        self.exit_margin_minutes = exit_margin_minutes
        self.size = size

        # State
        self.session = None
        self.rth_open = None
        self.range_high = None
        self.range_low = None
        self.range_complete = False
        self.atr = None
        self.entry_time = None
        self.entry_id = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)  # 1-minute bars for opening range and signals
        ctx.subscribe("1d")           # daily bars for ATR calculation

    def _compute_atr(self, ctx) -> None:
        """Compute average true range from the last `atr_lookback` daily bars."""
        daily = ctx.history("1d")
        if len(daily) < self.atr_lookback:
            self.atr = None
            return
        recent = daily[-self.atr_lookback:]
        ranges = [bar.high - bar.low for bar in recent]
        self.atr = sum(ranges) / len(ranges)

    def _reset_session(self, ctx) -> None:
        """Reset per-session state and compute RTH open and ATR."""
        self.session = ctx.session
        self.rth_open = ctx.session_start + 55_800_000_000_000  # 15.5 hours
        self.range_high = None
        self.range_low = None
        self.range_complete = False
        self.entry_time = None
        self.entry_id = None
        self._compute_atr(ctx)

    def _update_opening_range(self, bar) -> None:
        """Accumulate high/low from bars within the opening range window."""
        if self.rth_open is None:
            return
        if self.range_complete:
            return
        if bar.open_ts < self.rth_open:
            return
        if bar.open_ts >= self.rth_open + self.range_minutes * 60 * 1_000_000_000:
            self.range_complete = True
            return
        if self.range_high is None:
            self.range_high = bar.high
            self.range_low = bar.low
        else:
            self.range_high = max(self.range_high, bar.high)
            self.range_low = min(self.range_low, bar.low)

    def _exit_position(self, ctx) -> None:
        """Cancel bracket legs and flatten the position."""
        if self.entry_id is not None:
            legs = ctx.find_orders(parent_id=self.entry_id)
            for leg in legs:
                ctx.cancel(leg.id)
        ctx.close()
        self.entry_id = None
        self.entry_time = None

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.session:
            self._reset_session(ctx)

        # Process 1-minute bars
        if self.interval in ctx.bars:
            bar = ctx.bars[self.interval]
            self._update_opening_range(bar)

            # Check for breakout if range is complete and we are flat
            if (
                self.range_complete
                and self.atr is not None
                and self.atr > 0
                and self.range_high is not None
                and self.range_low is not None
                and not ctx.position
                and not ctx.working_order_ids
            ):
                threshold = self.breakout_mult * self.atr
                if bar.close > self.range_high + threshold:
                    self.entry_id = ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_mult * self.atr,
                        target_offset=self.target_mult * self.atr,
                        tag="orb_breakout_long",
                    )
                elif bar.close < self.range_low - threshold:
                    self.entry_id = ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_mult * self.atr,
                        target_offset=self.target_mult * self.atr,
                        tag="orb_breakout_short",
                    )

        # Time-based exit
        if (
            ctx.position
            and self.entry_time is not None
            and ctx.ts - self.entry_time > self.horizon_minutes * 60 * 1_000_000_000
        ):
            self._exit_position(ctx)

        # Session-end exit
        if (
            ctx.position
            and ctx.session_end - ctx.ts < self.exit_margin_minutes * 60 * 1_000_000_000
        ):
            self._exit_position(ctx)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position != 0 and self.entry_time is None:
            # This fill opened a position (entry fill)
            self.entry_time = ctx.ts
        elif fill.position == 0:
            # Position closed (stop, target, or manual exit)
            self.entry_time = None
            self.entry_id = None

    def on_lifecycle(self, ctx) -> None:
        event = ctx.event
        if event.order_id == self.entry_id and event.kind in (
            "day_expired",
            "cancel_confirmed",
            "order_rejected",
        ):
            self.entry_id = None
            self.entry_time = None
