"""
Order flow continuation with refined threshold and asymmetric entry sizing.

Analysis of 32 trials reveals the core pattern:
- Trial #1 (best): score -0.4823, ~35k trades
  - Adaptive volatility threshold (0.12) + multi-timeframe (1m + 15m)
  - Tight brackets (stop=0.8, target=1.2)
  - High selectivity = fewer trades, better quality

- Trial #32: score -0.8440, ~31k trades
  - Threshold raised to 0.13, slightly fewer trades
  - Score degraded from trial #1, suggesting 0.13 is too high

- Trial #31: score -0.8440, ~24k trades
  - Threshold 0.14 with sustained flow requirement
  - Over-filtered, worse score

Pattern: Trial #1's threshold of 0.12 is better than 0.13 or 0.14. The problem
is not the base threshold—it's that we need BETTER ENTRY TIMING within the
threshold.

New insight: Trial #1 trades both sides equally (size=2 for both long/short).
But order flow might not be symmetric:
- Buying pressure (delta > 0) might be more predictive than selling pressure
- Or vice versa—we should test asymmetry

Alternative hypothesis: Trial #1's brackets (stop=0.8, target=1.2) might be
slightly too loose. The stop gets hit on normal volatility, eating into gains.
Try tighter brackets (stop=0.6, target=1.0) to reduce whipsaws.

But first, let's test asymmetric sizing: if buying pressure is more reliable
than selling pressure, trade long more aggressively (size=2) and short more
conservatively (size=1). This increases exposure to the more reliable signal.

Strategy (asymmetric entry with trial #1's core):
1. Keep trial #1's exact logic (adaptive threshold 0.12 + multi-timeframe)
2. Keep tight brackets from trial #1 (stop=0.8, target=1.2)
3. CHANGE: Use size=2 for long entries (buying pressure), size=1 for short
4. This tests whether buying and selling pressure have different predictive power
5. If long is more reliable, this increases P&L; if not, it reduces it
6. The asymmetry should be visible in the trade count and P&L

The logic: Trial #1 is good. The threshold is right. But maybe the market
responds differently to buying vs selling pressure. Test this by sizing
asymmetrically: more aggressive on the side that works better.
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
        long_size: int = 2,
        short_size: int = 1,
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
            long_size: Position size for long entries (buying pressure)
            short_size: Position size for short entries (selling pressure)
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.long_size = long_size
        self.short_size = short_size
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

        # Get slow timeframe trend
        slow_window = slow_history.last(self.lookback_slow)
        slow_delta = 0.0
        slow_volume = 0.0

        for bar in slow_window:
            slow_delta += bar.delta
            slow_volume += bar.volume

        if slow_volume <= 0:
            return

        slow_imbalance = slow_delta / slow_volume

        # Entry logic (asymmetric sizing version of trial #1):
        # - Fast imbalance must exceed threshold (same as trial #1)
        # - Slow imbalance must be in same direction (confirming trend)
        # - CHANGE: Use different sizes for long vs short
        #   Long (buying pressure): size=2 (more aggressive)
        #   Short (selling pressure): size=1 (more conservative)
        # This tests whether buying pressure is more reliable than selling pressure

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            # Trade long more aggressively
            ctx.buy(
                size=self.long_size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            # Trade short more conservatively
            ctx.sell(
                size=self.short_size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
