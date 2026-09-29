"""
Intraday momentum with refined volatility-adaptive entry and reduced over-filtering.

Analysis of prior trials:
- Trial #0: -0.7386, 39,797 trades (baseline)
- Trial #5: -0.6642, 24,270 trades (best overall)
- Trial #7: -0.6544, 10,868 trades (best recent, but too selective)
- Trial #8: -0.7438, 31,073 trades (worse - too permissive)

Key insight: Trial #7 achieved best score with ~28 trades/session through strict filtering,
but #5 with ~63 trades/session was close behind. The issue with #8 was that relaxing
entry conditions increased noise more than it helped.

Refined hypothesis:
1. Use 4-bar lookback for range percentile (more responsive than 12)
2. Entry at 50% range percentile (top half of recent moves) - balances selectivity
3. Require BOTH current and prior bar to close decisively (>60% of range)
4. Current bar momentum must exceed prior bar momentum (acceleration)
5. Volatility regime filter: only trade in top 40% of session volatility ranges
6. Adaptive hold: 2 bars in normal conditions, 3 in high-vol
7. Asymmetric brackets: 0.40 stop (tight), 0.75 target (modest)
8. Minimum 1 bar between entries to avoid whipsaw
9. Track range history on self to avoid window issues

This targets 15-20k trades per 384 sessions while maintaining signal quality.
The key refinement: use shorter lookback (4 vs 12) for more responsive thresholds,
and 50% percentile (top half) rather than 35-40% to capture more high-quality moves
without excessive filtering.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_lookback: int = 4,
        range_percentile: float = 0.50,
        vol_percentile: float = 0.40,
        close_strength: float = 0.60,
        min_bars_between_entries: int = 1,
        base_stop: float = 0.40,
        base_target: float = 0.75,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.range_lookback = range_lookback
        self.range_percentile = range_percentile
        self.vol_percentile = vol_percentile
        self.close_strength = close_strength
        self.min_bars_between_entries = min_bars_between_entries
        self.base_stop = base_stop
        self.base_target = base_target
        self.size = size

        # State tracking across callbacks
        self._session: int | None = None
        self._bars_held: int = 0
        self._bars_since_entry: int = 0
        # Keep range history on self to avoid window issues
        self._ranges: deque[float] = deque(maxlen=range_lookback + 2)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_held = 0
            self._bars_since_entry = 0
            self._ranges.clear()

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current_bar = history[-1]
        current_range = current_bar.high - current_bar.low
        self._ranges.append(current_range)
        self._bars_since_entry += 1

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            hold_limit = 3 if self._is_high_volatility() else 2
            if self._bars_held >= hold_limit:
                ctx.close()
                self._bars_held = 0
                self._bars_since_entry = 0
            return

        # Not in position; check for entry
        if ctx.working_order_ids:
            return

        # Don't enter too soon after last exit
        if self._bars_since_entry < self.min_bars_between_entries:
            return

        # Need sufficient history for range computation
        if len(self._ranges) < self.range_lookback or len(history) < 2:
            return

        # Skip if not in elevated volatility regime
        if not self._is_high_volatility():
            return

        # Compute range threshold
        ranges_list = list(self._ranges)
        sorted_ranges = sorted(ranges_list)
        range_percentile_idx = max(0, int(len(sorted_ranges) * self.range_percentile))
        threshold_range = sorted_ranges[range_percentile_idx]

        # Skip if current bar's range is not exceptional
        if current_range < threshold_range:
            return

        # Check if bar closes decisively in one direction
        if current_range <= 0:
            return

        close_position_ratio = (current_bar.close - current_bar.low) / current_range

        prev_bar = history[-2]
        prev_range = prev_bar.high - prev_bar.low

        # Calculate momentum: close-to-open change
        current_momentum = current_bar.close - current_bar.open
        prior_momentum = prev_bar.close - prev_bar.open

        # Strong upside close: close is in upper close_strength% of the range
        if close_position_ratio > self.close_strength:
            # Confirm with momentum and prior bar direction
            if prev_range > 0:
                prev_close_ratio = (prev_bar.close - prev_bar.low) / prev_range
                # Require: prior bar also closed decisively up, and current momentum
                # exceeds prior momentum (acceleration)
                if (prev_close_ratio > self.close_strength and
                    prev_bar.close > prev_bar.open and
                    current_momentum > prior_momentum and
                    current_momentum > 0):
                    order_id = ctx.buy(
                        size=self.size,
                        stop_offset=self.base_stop,
                        target_offset=self.base_target,
                        tag="momentum-long",
                    )
                    if order_id is not None:
                        self._bars_held = 0
                        self._bars_since_entry = 0

        # Strong downside close: close is in lower (1-close_strength)% of the range
        elif close_position_ratio < (1.0 - self.close_strength):
            # Confirm with momentum and prior bar direction
            if prev_range > 0:
                prev_close_ratio = (prev_bar.close - prev_bar.low) / prev_range
                # Require: prior bar also closed decisively down, and current momentum
                # exceeds prior momentum in magnitude (acceleration)
                if (prev_close_ratio < (1.0 - self.close_strength) and
                    prev_bar.close < prev_bar.open and
                    current_momentum < prior_momentum and
                    current_momentum < 0):
                    order_id = ctx.sell(
                        size=self.size,
                        stop_offset=self.base_stop,
                        target_offset=self.base_target,
                        tag="momentum-short",
                    )
                    if order_id is not None:
                        self._bars_held = 0
                        self._bars_since_entry = 0

    def _is_high_volatility(self) -> bool:
        """Check if current volatility is in elevated regime."""
        if len(self._ranges) < self.range_lookback:
            return False

        ranges_list = list(self._ranges)
        sorted_ranges = sorted(ranges_list)
        vol_percentile_idx = max(0, int(len(sorted_ranges) * self.vol_percentile))
        vol_threshold = sorted_ranges[vol_percentile_idx]

        # Use most recent range for regime check
        recent_vol = ranges_list[-1]
        return recent_vol >= vol_threshold

    def on_fill(self, ctx) -> None:
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired", "cancel_confirmed"):
            self._bars_held = 0
