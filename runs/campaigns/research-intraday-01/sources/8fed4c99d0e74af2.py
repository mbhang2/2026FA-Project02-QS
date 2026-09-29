"""
Intraday momentum strategy with adaptive volatility thresholds and regime awareness.

Key insights from prior trials (#0: -0.7386, #1: -0.7727, #2: crashed):
1. Trial #0 had the best score, suggesting the core momentum idea has merit
2. The crash in #2 was from a malformed close() call with tag parameter
3. Both #0 and #1 were negative, indicating the strategy is losing to friction
4. The hypothesis is sound but needs better signal filtering to reduce false entries

New approach:
1. Use a simpler, more robust signal: the bar's true range relative to session median
2. Reduce position frequency to lower commission costs (key issue at 0.1 points/side)
3. Enter only on the strongest signals (top quartile of moves)
4. Tighter brackets to reduce slippage and exit faster
5. Skip entries when volatility is too low (less predictable)
6. Better confirmation: require the entry bar itself to show momentum, not just size

The refined hypothesis: large intrabar moves (high-low range) that are in the top
quartile of recent session volatility, when the bar closes in the direction of
that range, indicate continuation momentum. Entry on next bar if it opens favorably.
Minimize position count to reduce friction impact.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 14,
        range_percentile: float = 0.75,  # top 25% of recent ranges
        min_volatility: float = 0.3,  # skip when ranges are too small
        hold_bars: int = 2,  # shorter hold to reduce whipsaws
        stop_offset: float = 0.65,  # tighter stop
        target_offset: float = 1.0,  # modest target
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.range_percentile = range_percentile
        self.min_volatility = min_volatility
        self.hold_bars = hold_bars
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

        # State tracking
        self._session: int | None = None
        self._bars_held: int = 0
        self._ranges: deque[float] = deque(maxlen=lookback + 1)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_held = 0
            self._ranges.clear()

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        # Track ranges
        current_bar = history[-1]
        current_range = current_bar.high - current_bar.low
        self._ranges.append(current_range)

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.hold_bars:
                ctx.close()
                self._bars_held = 0
            return

        # Not in position; check for entry
        if ctx.working_order_ids:
            return

        # Need enough history
        if len(self._ranges) < self.lookback:
            return

        # Compute range threshold
        ranges_list = list(self._ranges)
        sorted_ranges = sorted(ranges_list)
        idx = int(len(sorted_ranges) * self.range_percentile)
        idx = min(idx, len(sorted_ranges) - 1)
        threshold_range = sorted_ranges[idx]

        # Skip if volatility is too low
        if threshold_range < self.min_volatility:
            return

        # Get the bar that just closed (most recent)
        curr_bar = history[-1]
        prev_bar = history[-2]

        # Signal: current bar has a large range AND closed in the direction of strength
        curr_range = current_range
        
        # Check if current bar's range is in top percentile
        if curr_range < threshold_range:
            return

        # Determine direction: did the bar close up or down?
        # Also check that it closed in the direction of the range (not a reversal)
        bar_open_mid = (curr_bar.high + curr_bar.low) / 2
        
        if curr_bar.close > bar_open_mid:
            # Bar closed in upper half; expect continuation up
            order_id = ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-long",
            )
            if order_id is not None:
                self._bars_held = 0
                
        elif curr_bar.close < bar_open_mid:
            # Bar closed in lower half; expect continuation down
            order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-short",
            )
            if order_id is not None:
                self._bars_held = 0

    def on_fill(self, ctx) -> None:
        # Reset hold counter on fill
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # Handle order lifecycle events
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired"):
            self._bars_held = 0
