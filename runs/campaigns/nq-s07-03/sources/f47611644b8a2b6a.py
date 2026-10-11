"""Opening range momentum with prior-session volatility scaling.

This strategy tests the "move holds" account: when price leaves the opening
range, the move continues. It is the mirror of the fade strategies that have
dominated this campaign, but with a volatility measure taken from prior
sessions rather than the current session's intraday ATR.

The opening range is the first 30 minutes of the regular-hours session. A
breakout is a 1-minute bar closing beyond the range by at least
`breakout_vol_mult` times the average daily range of the previous
`vol_lookback_sessions` sessions. The entry is in the direction of the
breakout, and the order-flow filter requires the breakout bar's signed volume
to be strongly aligned with the move (delta > 0.3 * volume for upside,
delta < -0.3 * volume for downside). This is the opposite of the fade filter,
which skipped such bars.

The bracket is scaled by the same prior-session volatility: stop at
`stop_vol_mult` and target at `target_vol_mult` times the average daily range.
Trading is restricted to a window after the range is ready (20 to 150 minutes)
to avoid the initial noisy period and capture the move that develops later.
At most one entry per session, with a 100-minute maximum hold and a 5-minute
session-end margin.

The volatility measure is computed from completed sessions only, so it is
available at the start of the current session without lookahead. The first few
sessions of the run are used to build the volatility history and no trades are
taken until at least `min_sessions_for_vol` prior sessions have been observed.
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
        vol_lookback_sessions: int = 5,
        min_sessions_for_vol: int = 5,
        breakout_vol_mult: float = 0.5,
        stop_vol_mult: float = 1.0,
        target_vol_mult: float = 2.0,
        max_hold_minutes: int = 100,
        min_wait_after_range_minutes: int = 20,
        trade_window_minutes: int = 150,
        min_range_width_vol_mult: float = 0.5,
        max_breakout_imbalance: float = 0.3,
        session_close_margin_minutes: int = 5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.opening_range_bars = opening_range_minutes  # 1m bars
        self.vol_lookback_sessions = vol_lookback_sessions
        self.min_sessions_for_vol = min_sessions_for_vol
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.max_hold_ns = max_hold_minutes * NS_PER_MINUTE
        self.min_wait_after_range_ns = min_wait_after_range_minutes * NS_PER_MINUTE
        self.trade_window_ns = trade_window_minutes * NS_PER_MINUTE
        self.min_range_width_vol_mult = min_range_width_vol_mult
        self.max_breakout_imbalance = max_breakout_imbalance
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

        # Volatility state: daily ranges of completed sessions
        self.daily_ranges: deque[float] = deque(maxlen=vol_lookback_sessions)
        self.session_high: Optional[float] = None
        self.session_low: Optional[float] = None

        # Trade state
        self.entry_time: Optional[int] = None
        self.entry_order_id: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, ctx) -> None:
        """Finalize previous session's range and reset per-session state."""
        # Finalize previous session if it exists and we have high/low
        if self.current_session is not None and self.session_high is not None and self.session_low is not None:
            daily_range = self.session_high - self.session_low
            if daily_range > 0:
                self.daily_ranges.append(daily_range)

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
        self.session_high = None
        self.session_low = None

    def _update_session_high_low(self, bar) -> None:
        """Track the high and low of the current session for daily range."""
        if self.session_high is None:
            self.session_high = bar.high
            self.session_low = bar.low
        else:
            self.session_high = max(self.session_high, bar.high)
            self.session_low = min(self.session_low, bar.low)

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

    def _avg_daily_range(self) -> float:
        """Average daily range over the lookback window of completed sessions."""
        if len(self.daily_ranges) < self.min_sessions_for_vol:
            return 0.0
        return sum(self.daily_ranges) / len(self.daily_ranges)

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

    def _manage_open_position(self, ctx) -> None:
        """Manage time-based exits."""
        if self.entry_time is not None and ctx.ts - self.entry_time > self.max_hold_ns:
            self._exit_position(ctx)
            return
        if ctx.session_end - ctx.ts < self.session_close_margin_ns:
            self._exit_position(ctx)
            return

    def on_bars(self, ctx) -> None:
        # Session change detection
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        # Only process our interval
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # Update session high/low (always, even during warmup)
        self._update_session_high_low(bar)

        # Update opening range
        self._update_opening_range(bar)

        # Trading logic only when actionable
        if not ctx.actionable:
            return

        # Manage existing position
        if ctx.position:
            self._manage_open_position(ctx)
            return

        # No position, no working orders: consider new entry
        if ctx.working_order_ids:
            return
        if self.entry_taken:
            return
        if not self.range_ready or self.range_high is None or self.range_low is None:
            return

        vol = self._avg_daily_range()
        if vol <= 0:
            return

        # Time filter: only trade after min_wait_after_range and within trade_window
        time_since_range_ready = bar.close_ts - self.range_ready_ts
        if time_since_range_ready < self.min_wait_after_range_ns:
            return
        if time_since_range_ready > self.trade_window_ns:
            return

        # Opening range width filter: require range to be at least min_range_width_vol_mult * vol
        range_width = self.range_high - self.range_low
        if range_width < self.min_range_width_vol_mult * vol:
            return

        threshold = self.breakout_vol_mult * vol
        stop_offset = self.stop_vol_mult * vol
        target_offset = self.target_vol_mult * vol
        volume = max(1, bar.volume)

        # Momentum entry: buy on upside breakout, sell on downside breakout.
        # Require the breakout bar's signed volume to be strongly aligned with
        # the move (delta > max_breakout_imbalance * volume for upside,
        # delta < -max_breakout_imbalance * volume for downside).
        if bar.close > self.range_high + threshold:
            if bar.delta <= self.max_breakout_imbalance * volume:
                return
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="momentum_long",
            )
            if order_id is not None:
                self.entry_taken = True
                self.entry_order_id = order_id
        elif bar.close < self.range_low - threshold:
            if bar.delta >= -self.max_breakout_imbalance * volume:
                return
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="momentum_short",
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
            # Position closed
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
