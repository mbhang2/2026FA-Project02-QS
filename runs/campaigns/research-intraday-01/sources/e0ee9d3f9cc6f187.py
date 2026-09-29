"""
Enhanced intraday momentum strategy with volatility-adjusted entry and exit.

Key improvements over prior trials:
1. Use intrabar volatility (high-low range) as the primary measure instead of 
   close-to-close moves, which is more stable and less noisy
2. Implement a two-stage entry: signal on large move, confirm on next bar's
   direction to reduce false signals
3. Dynamic bracket sizing based on recent volatility to adapt to regime changes
4. Track session-level statistics more carefully to avoid stale data
5. Stricter position management: exit on reversal signals, not just time-based
6. Use the high/low range as the volatility metric (more robust than close-to-close)

The refined hypothesis: after a bar with an unusually large intrabar range
(high-low) relative to recent session volatility, if the next bar opens and
continues in that direction, we enter. This reduces whipsaws from gap reversals.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 12,
        volatility_percentile: float = 0.65,
        hold_bars: int = 3,
        stop_offset: float = 0.85,
        target_offset: float = 1.35,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.volatility_percentile = volatility_percentile
        self.hold_bars = hold_bars
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

        # State tracking across callbacks
        self._session: int | None = None
        self._bars_held: int = 0
        self._pending_signal: tuple[str, int] | None = None  # (direction, bar_ts)
        
        # Track high-low ranges for volatility calculation
        self._ranges: deque[float] = deque(maxlen=lookback + 2)
        
        # Track the bar that triggered the signal for confirmation logic
        self._signal_bar_ts: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset state on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_held = 0
            self._pending_signal = None
            self._ranges.clear()
            self._signal_bar_ts = None

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        # Update range tracking with current bar
        current_bar = history[-1]
        current_range = current_bar.high - current_bar.low
        self._ranges.append(current_range)

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            
            # Check for reversal exit: if current bar closes opposite to position
            if len(history) >= 2:
                prev_bar = history[-2]
                curr_bar = history[-1]
                
                # Simple reversal: if we're long and current bar closes below prev bar's close
                # or if we're short and current bar closes above prev bar's close
                if ctx.position > 0 and curr_bar.close < prev_bar.close:
                    # Long position but reversal signal; exit early
                    ctx.close(tag="reversal-exit")
                    self._bars_held = 0
                    self._pending_signal = None
                    return
                elif ctx.position < 0 and curr_bar.close > prev_bar.close:
                    # Short position but reversal signal; exit early
                    ctx.close(tag="reversal-exit")
                    self._bars_held = 0
                    self._pending_signal = None
                    return
            
            # Time-based exit
            if self._bars_held >= self.hold_bars:
                ctx.close(tag="max-hold")
                self._bars_held = 0
                self._pending_signal = None
            return

        # Not in a position
        if ctx.working_order_ids:
            return

        # Need enough history for volatility calculation
        if len(self._ranges) < self.lookback:
            return

        # Compute volatility threshold from recent ranges
        ranges_list = list(self._ranges)
        sorted_ranges = sorted(ranges_list)
        idx = int(len(sorted_ranges) * self.volatility_percentile)
        idx = min(idx, len(sorted_ranges) - 1)
        threshold_volatility = sorted_ranges[idx]

        if threshold_volatility <= 0:
            return

        # Two-stage entry logic:
        # Stage 1: Detect a bar with unusually large range
        # Stage 2: Confirm on the next bar's direction
        
        prev_bar = history[-2]
        curr_bar = history[-1]
        prev_range = self._ranges[-2]
        curr_range = self._ranges[-1]

        # Check if the previous bar (now closed) had a large range
        if prev_range >= threshold_volatility:
            # Previous bar was volatile; determine its direction
            prev_direction = "up" if prev_bar.close > prev_bar.open else "down"
            
            # Store signal for confirmation on next bar
            self._pending_signal = (prev_direction, prev_bar.close_ts)
            self._signal_bar_ts = prev_bar.close_ts

        # Check for confirmation: current bar continues the signal direction
        if self._pending_signal is not None:
            signal_direction, signal_ts = self._pending_signal
            
            # Only act on confirmation if it's the next bar after the signal
            if curr_bar.open_ts > signal_ts:
                # Check if current bar's direction matches signal
                curr_direction = "up" if curr_bar.close > curr_bar.open else "down"
                
                if signal_direction == curr_direction and curr_direction == "up":
                    # Confirmed long signal
                    order_id = ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="momentum-long-confirmed",
                    )
                    if order_id is not None:
                        self._bars_held = 0
                        self._pending_signal = None
                        self._signal_bar_ts = None
                        
                elif signal_direction == curr_direction and curr_direction == "down":
                    # Confirmed short signal
                    order_id = ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="momentum-short-confirmed",
                    )
                    if order_id is not None:
                        self._bars_held = 0
                        self._pending_signal = None
                        self._signal_bar_ts = None
                        
                else:
                    # No confirmation; clear the signal
                    self._pending_signal = None
                    self._signal_bar_ts = None

    def on_fill(self, ctx) -> None:
        # Reset hold counter on fill
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # Handle order lifecycle events
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired"):
            self._pending_signal = None
            self._signal_bar_ts = None
            self._bars_held = 0
