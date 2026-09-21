"""
Order flow continuation with dynamic entry filtering and risk-adjusted position sizing.

Analysis of 34 trials reveals a critical insight:

Trial #1 (best): score -0.4823, ~35k trades
- Adaptive volatility threshold (0.12) + multi-timeframe (1m + 15m)
- Brackets (stop=0.8, target=1.2)
- Lookbacks: fast=5, slow=4
- Size=2 for both sides

Trial #34 (recent): score -0.5198, ~30k trades
- Tightened brackets (stop=0.5, target=0.8)
- Added slow confirmation magnitude filter (0.02)
- WORSE than #1, suggesting over-filtering

Key pattern across all trials:
- Scores range from -0.4823 to -0.9738
- Best scores (< -0.6) have moderate trade counts (30-60k)
- Over-filtering (fewer trades) tends to worsen scores
- Trial #1 remains best despite simplicity

Critical realization: The market might require FASTER reaction to flow signals.
- Trial #1 uses 1m bars (reasonable)
- But the lookback=5 means we measure flow over 5 minutes
- By the time we enter, the flow might have already reversed
- A 30-second bar might capture flow signals faster
- Or we need to enter on CURRENT bar flow, not accumulated flow

New hypothesis: The issue is TIMING, not filtering.
- Current approach: measure flow over N bars, then enter
- Problem: by the time entry fires, the move might be half over
- Solution: weight RECENT flow more heavily, or enter on CURRENT bar flow
- Use exponential weighting: recent bars count more than old bars

Alternative: The slow confirmation might be TOO STRICT.
- Trial #34 required slow_imbalance > 0.02 (small magnitude)
- Trial #1 required slow_imbalance > 0 (any positive)
- Maybe we should use WEAK slow confirmation but STRONGER fast threshold
- High fast threshold (0.15+) filters false signals without needing slow strength

Strategy: FAST THRESHOLD + WEAK SLOW CONFIRMATION
1. Keep trial #1's core (1m + 15m, lookbacks 5/4)
2. CHANGE: Raise fast threshold to 0.15 (from 0.12)
   - This is MORE selective on fast flow, requiring stronger signals
   - Fewer false entries from noise
3. CHANGE: Relax slow confirmation to ANY positive/negative (no magnitude)
   - Slow just confirms direction, doesn't need strength
   - This keeps entries flowing while fast threshold filters
4. CHANGE: Revert to trial #1's brackets (stop=0.8, target=1.2)
   - Trial #1 was best; tightening in #34 made it worse
5. Keep size=2 (symmetric)

Expected outcome: Fewer trades than #1 (~25-30k) due to higher threshold, but:
- Each trade has stronger fast signal (0.15 vs 0.12)
- Directional confirmation from slow (any magnitude)
- Better entry quality = better win rate
- Revert to proven brackets (0.8/1.2)
- Net: Should beat #1's -0.4823

This is THRESHOLD OPTIMIZATION: find the sweet spot between signal strength and
entry frequency.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.15,
        volatility_window: int = 20,
        size: int = 2,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Base imbalance threshold (raised to 0.15 for selectivity)
            volatility_window: Bars to measure volatility over
            size: Position size
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        # Skip if we already have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Skip during warmup when orders would be ignored
        if not ctx.actionable:
            return

        # Only act when the fast bar closes
        if self.fast_interval not in ctx.bars:
            return

        fast_history = ctx.history(self.fast_interval)
        slow_history = ctx.history(self.slow_interval)

        # Need enough history for both calculations
        if len(fast_history) < self.lookback_fast:
            return
        if len(slow_history) < self.lookback_slow:
            return

        # Calculate adaptive threshold based on recent volatility
        volatility_lookback = min(self.volatility_window, len(fast_history))
        if volatility_lookback > 0:
            vol_window = fast_history.last(volatility_lookback)
            price_range = max(b.high for b in vol_window) - min(b.low for b in vol_window)
            avg_price = sum(b.close for b in vol_window) / len(vol_window)
            if avg_price > 0:
                volatility_ratio = price_range / avg_price
            else:
                volatility_ratio = 1.0
            # Adapt threshold: higher volatility = higher threshold needed
            threshold = self.base_threshold * (0.8 + 0.4 * volatility_ratio)
        else:
            threshold = self.base_threshold

        # Get the fast timeframe lookback window for flow measurement
        fast_window = fast_history.last(self.lookback_fast)

        # Calculate signed order flow on fast timeframe
        total_delta = 0.0
        total_volume = 0.0

        for bar in fast_window:
            total_delta += bar.delta
            total_volume += bar.volume

        if total_volume <= 0:
            return

        # Normalize imbalance
        fast_imbalance = total_delta / total_volume

        # Get slow timeframe trend for directional confirmation only
        slow_window = slow_history.last(self.lookback_slow)
        slow_delta = 0.0
        slow_volume = 0.0

        for bar in slow_window:
            slow_delta += bar.delta
            slow_volume += bar.volume

        if slow_volume <= 0:
            return

        slow_imbalance = slow_delta / slow_volume

        # Entry logic (higher threshold, weak slow confirmation):
        # - Fast imbalance must exceed higher threshold (0.15 vs 0.12)
        #   This filters out marginal signals, requiring stronger flow
        # - Slow imbalance just needs to be in same direction (no magnitude requirement)
        #   This confirms trend without over-filtering
        # - Revert to trial #1's proven brackets (stop=0.8, target=1.2)
        # Expected: fewer trades, higher quality, better P&L

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow trend
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow trend
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
