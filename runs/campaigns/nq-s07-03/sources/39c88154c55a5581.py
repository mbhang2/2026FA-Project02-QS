"""Opening range strategy: fade weak breakouts, follow strongly confirmed ones.

Investigates whether price leaving the opening range tends to revert (range wins)
or hold (move holds). The opening range is the first 30 minutes of regular trading
hours (RTH), defined as the high and low of the first 30 one-minute bars after
15.5 hours from the Globex session start. Volatility is measured by average true
range (ATR) over the last 150 one-minute bars.

A breakout must close beyond the range by 0.8 ATR. The order flow on the breakout
bar decides the side:

- If the bar's signed volume (`bar.delta`) is strongly aligned with the departure
  (`strong_flow_mult = 0.40` of volume), we treat the move as likely to hold and
  enter a momentum order in the direction of the breakout.
- If the signed volume is weakly aligned with the departure
  (`max_breakout_imbalance = 0.20` of volume), we sit out: the departure is too
  one-sided to fade but not one-sided enough to follow.
- Otherwise the flow is mixed or counter-flow, and we fade the departure.

This keeps all other filters from the best prior fade-only variant: a meaningful
opening range width, a non-volatile breakout bar, a single trade per session, a
0.8 ATR protective stop, a 3.0 ATR target, a 100-minute maximum hold, and a forced
exit near the session close. Entries are only considered between 20 and 110
minutes after the opening range is complete.
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
        strong_flow_mult: float = 0.40,
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
        self.strong_flow_mult = strong_flow_mult
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

        # Volatility state (ATR)
        self.last_close: Optional[float] = None
        self.true_ranges: deque[float] = deque(maxlen=2000)

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
        # a true range from the previous session's close (avoids overnight gap).
        self.last_close = None
        self.true_ranges.clear()

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
        """Update the ATR buffer with the current bar's true range."""
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

    def _atr(self) -> float:
        """Compute average true range over the lookback window."""
        if len(self.true_ranges) < self.vol_lookback:
            return 0.0
        recent = list(self.true_ranges)[-self.vol_lookback:]
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
            return  # let bracket manage

        # No position, no working orders: consider new entry
        if ctx.working_order_ids:
            return
        if self.entry_taken:
            return
        if not self.range_ready or self.range_high is None or self.range_low is None:
            return
        if len(self.true_ranges) < self.vol_lookback:
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

        order_id = None

        if bar.close > self.range_high + threshold:
            # Strongly aligned breakout: the departure is likely to hold.
            if bar.delta > self.strong_flow_mult * volume:
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="momentum_long",
                )
            # Weakly aligned breakout: too one-sided to fade, not one-sided
            # enough to follow. Stay out.
            elif bar.delta > self.max_breakout_imbalance * volume:
                return
            # Mixed or counter-flow breakout: the range wins, fade it.
            else:
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="fade_short",
                )
        elif bar.close < self.range_low - threshold:
            # Strongly aligned downside breakout: the departure is likely to hold.
            if bar.delta < -self.strong_flow_mult * volume:
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="momentum_short",
                )
            elif bar.delta < -self.max_breakout_imbalance * volume:
                return
            else:
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
        if (
            self.entry_order_id is not None
            and event.order_id == self.entry_order_id
            and event.kind in ("day_expired", "cancel_confirmed", "order_rejected")
        ):
            self.entry_order_id = None
            self.entry_time = None
