"""
Order flow continuation with tighter entry filtering and optimized brackets.

Analysis of 33 trials reveals critical insights:

Trial #1 (best): score -0.4823, ~35k trades
- Adaptive volatility threshold (0.12) + multi-timeframe (1m + 15m)
- Tight brackets (stop=0.8, target=1.2)
- Lookbacks: fast=5, slow=4
- Size=2 for both sides

Trial #33 (asymmetric): score -0.6586, ~60k trades
- Same core as #1 but with asymmetric sizing (long=2, short=1)
- WORSE score despite similar trade count
- Suggests asymmetry is NOT the answer

Trial #32: score -0.8440, ~31k trades
- Threshold raised to 0.13
- Slightly worse than #1, confirming 0.12 is better

Pattern: Trial #1's core logic is sound. The degradation in #33 (asymmetry)
and #32 (higher threshold) suggests we need a DIFFERENT kind of improvement.

Key observation: Trial #1 trades ~35k times with score -0.4823. That's still
negative. The issue is not trade frequency—it's that each trade loses money on
average (likely due to commission drag).

New hypothesis: The problem is NOT the entry logic, but the EXIT logic.
- Trial #1's brackets (stop=0.8, target=1.2) might be too loose
- A stop of 0.8 gets hit on normal volatility, crystallizing losses
- A target of 1.2 requires a 1.2-point move to win—hard to achieve consistently
- Result: many small losses, fewer big wins = negative P&L

Strategy change: TIGHTEN THE BRACKETS
- Reduce stop_offset from 0.8 to 0.5 (tighter stop, fewer whipsaws)
- Reduce target_offset from 1.2 to 0.8 (more achievable target)
- This reduces the risk/reward ratio but increases win rate
- Commission is 0.2 per round turn, so we need tighter management

Also: Trial #1 might be over-trading slightly. The slow imbalance confirmation
is directional (>0 or <0) but doesn't require STRENGTH. A slow imbalance of
0.001 (barely positive) still triggers. Add a minimum magnitude check.

Refined strategy:
1. Keep trial #1's core (threshold=0.12, lookbacks 5/4, 1m/15m)
2. CHANGE: Tighter brackets (stop=0.5, target=0.8)
3. CHANGE: Require slow imbalance to have minimum magnitude (>0.02 or <-0.02)
   - This filters weak slow confirmation, reducing false entries
   - Still allows strong flow signals in either direction
4. Keep size=2 (trial #33's asymmetry hurt, so stay symmetric)

Expected outcome: Fewer trades (~25-30k), but each trade has:
- Tighter risk (0.5 vs 0.8 stop)
- More achievable target (0.8 vs 1.2)
- Stronger slow confirmation (magnitude filter)
- Better win rate, lower loss magnitude
- Net: better P&L despite fewer trades

This is a QUALITY OVER QUANTITY approach: trade less, but trade better.
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
        stop_offset: float = 0.5,
        target_offset: float = 0.8,
        slow_confirmation_min: float = 0.02,
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
            stop_offset: Stop loss distance from entry (tightened to 0.5)
            target_offset: Profit target distance from entry (tightened to 0.8)
            slow_confirmation_min: Minimum magnitude of slow imbalance to confirm
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
        self.slow_confirmation_min = slow_confirmation_min

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

        # Entry logic (tighter brackets, stronger slow confirmation):
        # - Fast imbalance must exceed threshold (same as trial #1)
        # - Slow imbalance must be in same direction AND have minimum magnitude
        # - This filters weak slow confirmation, reducing marginal entries
        # - Tighter brackets (stop=0.5, target=0.8) reduce risk and increase achievability

        if fast_imbalance > threshold and slow_imbalance > self.slow_confirmation_min:
            # Strong buying pressure on fast, confirmed by slow with magnitude
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < -self.slow_confirmation_min:
            # Strong selling pressure on fast, confirmed by slow with magnitude
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
