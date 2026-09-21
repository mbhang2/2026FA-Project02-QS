"""
Order flow continuation with exponential-weighted fast imbalance and optimized slow confirmation.

Analysis of 35 trials reveals critical patterns:

Trial #1 (best): score -0.4823, ~35k trades
- Adaptive volatility threshold (0.12) + multi-timeframe (1m + 15m)
- Brackets (stop=0.8, target=1.2)
- Lookbacks: fast=5, slow=4
- Size=2

Trial #35 (recent): score -0.4881, ~27k trades
- Raised threshold to 0.15 (higher selectivity)
- Reverted to trial #1's brackets
- SLIGHTLY WORSE than #1

Key insight: Trial #1 remains best after 35 iterations. The issue is NOT
threshold tuning or bracket adjustment—those changes consistently make it worse.

The real problem: TIMING OF ENTRY.
- Trial #1 measures flow over 5 bars (5 minutes in 1m timeframe)
- By the time we enter, the flow signal is 2.5 minutes old on average
- In a fast market, the move might already be half over
- Solution: WEIGHT RECENT FLOW MORE HEAVILY

New approach: EXPONENTIAL WEIGHTING on fast imbalance
- Instead of equal weight on all 5 bars: [1, 1, 1, 1, 1]
- Use exponential weights: [1, 2, 4, 8, 16] (most recent counts 16x more)
- This captures CURRENT flow momentum, not historical average
- Reacts faster to flow changes, enters closer to the move start

Why this works:
1. Fast markets reward recency—yesterday's flow is less predictive than today's
2. Exponential weighting is a proven technique in momentum indicators (EMA, etc.)
3. Keeps the proven core (1m + 15m, lookbacks 5/4, brackets 0.8/1.2)
4. No new parameters to tune—just changes how we weight existing data
5. Should improve entry timing without increasing false signals

Implementation:
1. Keep trial #1's structure entirely (threshold=0.12, size=2, etc.)
2. CHANGE: Weight fast bars exponentially [1, 2, 4, 8, 16]
3. Keep slow imbalance simple (any positive/negative confirmation)
4. Keep brackets (stop=0.8, target=1.2)

Expected outcome: Better entry timing → more trades closer to move start →
better average P&L per trade → score better than #1's -0.4823.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.12,
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
            base_threshold: Base imbalance threshold (adjusted by volatility)
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

        # Calculate signed order flow with EXPONENTIAL WEIGHTING on fast timeframe
        # Weights: [1, 2, 4, 8, 16] for oldest to newest bar
        # This makes recent flow much more influential than old flow
        weights = [2 ** i for i in range(self.lookback_fast)]
        total_weighted_delta = 0.0
        total_weighted_volume = 0.0

        for bar, weight in zip(fast_window, weights):
            total_weighted_delta += bar.delta * weight
            total_weighted_volume += bar.volume * weight

        if total_weighted_volume <= 0:
            return

        # Normalize exponentially-weighted imbalance
        fast_imbalance = total_weighted_delta / total_weighted_volume

        # Get slow timeframe trend (simple average, no exponential weighting)
        slow_window = slow_history.last(self.lookback_slow)
        slow_delta = 0.0
        slow_volume = 0.0

        for bar in slow_window:
            slow_delta += bar.delta
            slow_volume += bar.volume

        if slow_volume <= 0:
            return

        slow_imbalance = slow_delta / slow_volume

        # Entry logic (exponential-weighted fast imbalance):
        # - Fast imbalance uses exponential weighting (recent bars count 16x more)
        # - This captures current momentum, not historical average
        # - Slow imbalance provides directional confirmation
        # - Same threshold and brackets as trial #1 (proven to work)

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast (exponentially-weighted), confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast (exponentially-weighted), confirmed by slow
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
