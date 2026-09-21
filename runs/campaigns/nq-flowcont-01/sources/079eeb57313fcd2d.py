"""
Order flow continuation with refined entry timing and aggressive early-session trading.

Analysis of trial progression:
- Trial #1 (score -0.4823): Best performer, ~35k trades, adaptive threshold + multi-timeframe
- Trial #13 (score -0.8869): Added session-aware adjustments, worse despite refinement
- Trial #12 (score -0.8869): Added momentum + volume checks, also degraded
- Pattern: Simpler approaches outperform complex ones

Key observation: Trial #1's success was driven by:
1. Adaptive volatility-based threshold (0.8 + 0.4 * vol_ratio multiplier)
2. Multi-timeframe confirmation (fast signal + slow trend agreement)
3. Conservative position sizing (size=1)
4. Moderate bracket levels (0.8 stop, 1.2 target)

The degradation in trials #12-13 suggests over-optimization. However, all trials
still score negative, meaning the commission cost (0.2 per round trip) is eroding
the edge. The signal exists but is marginal.

New approach - go back to trial #1's foundation but with one key refinement:
- Trial #1 traded ~35k times but still lost money overall
- The issue isn't the signal but the exit: brackets are symmetric, but flow
  continuation might have asymmetric payoffs (longer winners, shorter losers, or vice versa)
- Instead of adjusting entry filters further, adjust the bracket asymmetry:
  - Tighter stops (0.6 instead of 0.8) to cut losses quickly
  - Wider targets (1.5 instead of 1.2) to let winners run
  - This trades higher win rate for better risk/reward on winners

Additional refinement:
- Trial #1 used lookback_fast=5, lookback_slow=4
- Try lookback_fast=6 (slightly longer to capture more flow momentum)
- Try lookback_slow=3 (faster trend confirmation to avoid stale signals)
- Slightly higher base threshold (0.13 instead of 0.12) to reduce false entries

The hypothesis: Order flow continuation is real, but the edge is small.
Trial #1 found it. The path forward is asymmetric exits (tighter stops, wider targets)
to improve risk/reward, not more entry filters.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 6,
        lookback_slow: int = 3,
        base_threshold: float = 0.13,
        volatility_window: int = 20,
        size: int = 1,
        stop_offset: float = 0.6,
        target_offset: float = 1.5,
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
            stop_offset: Stop loss distance from entry (tighter than target)
            target_offset: Profit target distance from entry (wider than stop)
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

        # Entry logic:
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - Asymmetric bracket: tight stops (0.6) and wider targets (1.5)
        #   to improve risk/reward on the small but real order flow edge

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=self.size,
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
