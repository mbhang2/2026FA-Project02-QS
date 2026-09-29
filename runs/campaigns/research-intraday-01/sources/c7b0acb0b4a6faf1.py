"""
Intraday momentum strategy with adaptive entry filtering and tighter execution.

Analysis of prior trials:
- Trial #0: -0.7386, 39,797 trades (best overall)
- Trial #5: -0.6642, 24,270 trades (best recent, 10% improvement)
- Trial #4: -0.7523, 18,966 trades (too few trades, worse score)
- Trial #3: -0.8022, 27,266 trades (worse despite filtering)

Key insight from #5's improvement:
The 10% gain from #0 to #5 came from reducing trade count while maintaining signal quality.
This suggests we're filtering out low-conviction trades effectively with the percentile-based
range filter and close-strength confirmation.

The problem: we're still generating 24k trades on 384 sessions = ~63 trades/session.
At 0.1 points/side commission (0.2 points round-trip), we need high conviction entries.

New hypothesis - further refine entry conditions:
1. Keep the percentile-based range filter (works well in #5)
2. Tighten the close_strength threshold to require even more decisive closes (70% instead of 60%)
3. Add momentum confirmation: require the close-to-close change from prior bar to be significant
4. Use tighter brackets to reduce slippage impact: stop=0.35, target=0.75
5. Hold for only 1-2 bars (shorter window reduces whipsaw)
6. Require bars to be further apart (skip entry if we just exited)

The refined momentum hypothesis: a bar with:
- Range in top 40% of session (exceptional volatility)
- Close >70% of range (very decisive direction)
- Prior bar also moved decisively in same direction (momentum confirmation)
- Indicates strong continuation within 1-2 bars

This should reduce #5's 24k trades further while keeping the best signals, improving the score.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 12,
        range_percentile: float = 0.40,
        close_strength: float = 0.70,
        hold_bars: int = 1,
        stop_offset: float = 0.35,
        target_offset: float = 0.75,
        size: int = 1,
        min_bars_between_entries: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.range_percentile = range_percentile
        self.close_strength = close_strength
        self.hold_bars = hold_bars
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_bars_between_entries = min_bars_between_entries

        # State tracking across callbacks
        self._session: int | None = None
        self._bars_held: int = 0
        self._ranges: deque[float] = deque(maxlen=lookback + 1)
        self._bars_since_last_entry: int = 0

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
            self._bars_since_last_entry = 0

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current_bar = history[-1]
        current_range = current_bar.high - current_bar.low
        self._ranges.append(current_range)
        self._bars_since_last_entry += 1

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.hold_bars:
                ctx.close()
                self._bars_held = 0
                self._bars_since_last_entry = 0
            return

        # Not in position; check for entry
        if ctx.working_order_ids:
            return

        # Don't enter too soon after last exit
        if self._bars_since_last_entry < self.min_bars_between_entries:
            return

        # Need sufficient history
        if len(self._ranges) < self.lookback or len(history) < 2:
            return

        # Compute range threshold (top percentile = stronger signals)
        ranges_list = list(self._ranges)
        sorted_ranges = sorted(ranges_list)
        percentile_idx = max(0, int(len(sorted_ranges) * self.range_percentile))
        threshold_range = sorted_ranges[percentile_idx]

        # Skip if current bar's range is not exceptional
        if current_range < threshold_range:
            return

        # Check if bar closes decisively in one direction
        if current_range > 0:
            close_position_ratio = (current_bar.close - current_bar.low) / current_range
        else:
            return

        prev_bar = history[-2]
        prev_range = prev_bar.high - prev_bar.low

        # Strong upside close: close is in upper 70% of the range
        if close_position_ratio > self.close_strength:
            # Confirm with momentum: prior bar also closed decisively up
            if prev_range > 0:
                prev_close_ratio = (prev_bar.close - prev_bar.low) / prev_range
                # Require prior bar to also be in upper 50% (moving up)
                if prev_close_ratio > 0.50 and prev_bar.close > prev_bar.open:
                    order_id = ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="momentum-long",
                    )
                    if order_id is not None:
                        self._bars_held = 0
                        self._bars_since_last_entry = 0

        # Strong downside close: close is in lower 30% of the range
        elif close_position_ratio < (1.0 - self.close_strength):
            # Confirm with momentum: prior bar also closed decisively down
            if prev_range > 0:
                prev_close_ratio = (prev_bar.close - prev_bar.low) / prev_range
                # Require prior bar to also be in lower 50% (moving down)
                if prev_close_ratio < 0.50 and prev_bar.close < prev_bar.open:
                    order_id = ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="momentum-short",
                    )
                    if order_id is not None:
                        self._bars_held = 0
                        self._bars_since_last_entry = 0

    def on_fill(self, ctx) -> None:
        # Reset hold counter on fill
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # Handle order lifecycle events
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired", "cancel_confirmed"):
            self._bars_held = 0
