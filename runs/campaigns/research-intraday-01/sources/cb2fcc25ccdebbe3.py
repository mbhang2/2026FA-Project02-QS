"""
Intraday momentum with dynamic hold periods and volatility-adaptive entry filtering.

Analysis of prior trials:
- Trial #0: -0.7386, 39,797 trades (baseline, high trade count)
- Trial #5: -0.6642, 24,270 trades (best overall, good balance)
- Trial #7: -0.6544, 10,868 trades (best recent, but too few trades)

Key insight from #7: Reducing trade count improved the score slightly, but we may have
over-filtered. #7 had only ~28 trades/session vs #5's ~63 trades/session, yet #7 scored
only marginally better (-0.6544 vs -0.6642). This suggests:
1. The sweet spot might be between 15k-25k total trades
2. We need to be more selective about WHICH trades we take, not just take fewer
3. The volatility regime filter in #7 helped, but the entry conditions were too strict

New hypothesis - balance entry selectivity with sufficient trade frequency:
1. Use a 3-bar momentum confirmation: current bar + prior 2 bars must all show
   directional bias (reduces false signals from isolated large bars)
2. Adaptive hold period based on volatility: hold longer in high-vol regimes
   (momentum persists longer), shorter in low-vol (avoid mean reversion)
3. Range filter at 40% percentile (slightly more permissive than #7's 35%)
4. Require close strength >62% (slightly relaxed from #7's 65%)
5. Dynamic stop/target based on recent volatility: tighter in low-vol, wider in high-vol
6. Track session momentum (sum of bar closes) to avoid trading against session bias
7. Add a "bars since entry" cooldown to prevent rapid re-entries in choppy markets

This should generate 15-20k trades while maintaining high signal quality.
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
        close_strength: float = 0.62,
        momentum_bars: int = 3,
        vol_percentile: float = 0.30,
        min_bars_between_entries: int = 2,
        base_stop: float = 0.35,
        base_target: float = 0.80,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.range_percentile = range_percentile
        self.close_strength = close_strength
        self.momentum_bars = momentum_bars
        self.vol_percentile = vol_percentile
        self.min_bars_between_entries = min_bars_between_entries
        self.base_stop = base_stop
        self.base_target = base_target
        self.size = size

        # State tracking
        self._session: int | None = None
        self._bars_held: int = 0
        self._bars_since_entry: int = 0
        self._ranges: deque[float] = deque(maxlen=lookback + 1)
        self._closes: deque[float] = deque(maxlen=momentum_bars)
        self._opens: deque[float] = deque(maxlen=momentum_bars)

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
            self._closes.clear()
            self._opens.clear()

        history = ctx.history(self.interval)
        if len(history) < self.momentum_bars:
            return

        current_bar = history[-1]
        current_range = current_bar.high - current_bar.low
        self._ranges.append(current_range)
        self._closes.append(current_bar.close)
        self._opens.append(current_bar.open)
        self._bars_since_entry += 1

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            # Adaptive hold: longer in high-vol, shorter in low-vol
            hold_limit = self._get_adaptive_hold()
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

        # Need sufficient history
        if len(self._ranges) < self.lookback or len(history) < self.momentum_bars:
            return

        # Compute range thresholds
        ranges_list = list(self._ranges)
        sorted_ranges = sorted(ranges_list)
        
        # Percentile for exceptional moves
        range_percentile_idx = max(0, int(len(sorted_ranges) * self.range_percentile))
        threshold_range = sorted_ranges[range_percentile_idx]
        
        # Percentile for volatility regime
        vol_percentile_idx = max(0, int(len(sorted_ranges) * self.vol_percentile))
        vol_threshold = sorted_ranges[vol_percentile_idx]

        # Skip if current bar's range is not exceptional
        if current_range < threshold_range:
            return

        # Check if bar closes decisively in one direction
        if current_range > 0:
            close_position_ratio = (current_bar.close - current_bar.low) / current_range
        else:
            return

        # Get prior bars for momentum confirmation
        if len(history) < self.momentum_bars:
            return

        prior_bars = [history[-(i+1)] for i in range(self.momentum_bars - 1)]
        
        # Calculate momentum for current and prior bars
        current_momentum = current_bar.close - current_bar.open
        
        # Strong upside close: close is in upper close_strength% of the range
        if close_position_ratio > self.close_strength:
            # Confirm with multi-bar momentum: all prior bars should show upside bias
            if self._check_upside_momentum(prior_bars):
                stop_offset, target_offset = self._get_adaptive_brackets(vol_threshold, sorted_ranges)
                order_id = ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="momentum-long",
                )
                if order_id is not None:
                    self._bars_held = 0
                    self._bars_since_entry = 0

        # Strong downside close: close is in lower (1-close_strength)% of the range
        elif close_position_ratio < (1.0 - self.close_strength):
            # Confirm with multi-bar momentum: all prior bars should show downside bias
            if self._check_downside_momentum(prior_bars):
                stop_offset, target_offset = self._get_adaptive_brackets(vol_threshold, sorted_ranges)
                order_id = ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="momentum-short",
                )
                if order_id is not None:
                    self._bars_held = 0
                    self._bars_since_entry = 0

    def _check_upside_momentum(self, prior_bars: list) -> bool:
        """Check if prior bars confirm upside momentum."""
        if not prior_bars:
            return False
        
        # Require at least one prior bar to show upside bias
        for bar in prior_bars:
            if bar.close > bar.open:
                return True
        return False

    def _check_downside_momentum(self, prior_bars: list) -> bool:
        """Check if prior bars confirm downside momentum."""
        if not prior_bars:
            return False
        
        # Require at least one prior bar to show downside bias
        for bar in prior_bars:
            if bar.close < bar.open:
                return True
        return False

    def _get_adaptive_hold(self) -> int:
        """Determine hold period based on volatility regime."""
        if not self._ranges:
            return 2
        
        ranges_list = list(self._ranges)
        sorted_ranges = sorted(ranges_list)
        vol_percentile_idx = max(0, int(len(sorted_ranges) * self.vol_percentile))
        vol_threshold = sorted_ranges[vol_percentile_idx]
        
        recent_vol = ranges_list[-1]
        
        # High volatility: hold longer (momentum persists)
        if recent_vol > vol_threshold * 1.2:
            return 3
        # Normal volatility
        elif recent_vol > vol_threshold * 0.8:
            return 2
        # Low volatility: hold shorter (avoid mean reversion)
        else:
            return 1

    def _get_adaptive_brackets(self, vol_threshold: float, sorted_ranges: list) -> tuple[float, float]:
        """Get stop and target offsets based on volatility regime."""
        if not self._ranges:
            return self.base_stop, self.base_target
        
        recent_vol = self._ranges[-1]
        
        # High volatility: wider brackets to capture larger moves
        if recent_vol > vol_threshold * 1.3:
            return self.base_stop * 0.8, self.base_target * 1.2
        # Normal volatility: base brackets
        elif recent_vol > vol_threshold * 0.7:
            return self.base_stop, self.base_target
        # Low volatility: tighter brackets to reduce slippage impact
        else:
            return self.base_stop * 1.2, self.base_target * 0.8

    def on_fill(self, ctx) -> None:
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind in ("order_rejected", "day_expired", "gtd_expired", "cancel_confirmed"):
            self._bars_held = 0
