"""
Order flow continuation with entry confirmation and reduced over-trading.

Analysis of 31 trials reveals a critical pattern:
- Trial #1 (best): score -0.4823, ~35k trades
  - Core: adaptive volatility threshold (0.12) + multi-timeframe (1m + 15m)
  - Tight brackets (stop=0.8, target=1.2)
  - High selectivity = fewer trades, better quality

- Trial #31: score -0.8440, ~24k trades (recent attempt at tightening)
  - Tried to be MORE selective with sustained flow requirement
  - Reduced trades further but score got worse
  - Suggests we went TOO FAR in filtering

- Trials #28-30: -0.96 to -0.97, 100k-195k trades
  - Over-trading kills returns (commission drag)
  - Flow acceleration, momentum additions, regime filters all failed

Key insight from trial #31's failure: The sustained flow requirement (requiring
prior bar in same direction) was TOO STRICT. It filtered out valid entries that
had one-bar consolidation before continuing.

The real issue: Trial #1's threshold (0.12) might be slightly too low, causing
marginal flow signals to trigger. But making it TOO HIGH (0.14 in #31) filters
good trades.

New hypothesis: Trial #1 is nearly optimal. The improvement is TUNING, not
redesign:
1. Keep trial #1's exact structure (1m fast, 15m slow, adaptive threshold)
2. Keep trial #1's brackets (stop=0.8, target=1.2)
3. CHANGE: Optimize the base threshold (0.12) more carefully
   - Trial #1 traded ~35k times with threshold=0.12
   - Trial #31 traded ~24k times with threshold=0.14 (worse score)
   - Try threshold=0.13 (middle ground)
4. Require slow imbalance confirmation to be DIRECTIONAL (not zero), but not
   require magnitude > 0.05 (that was too strict in #31)
5. Keep the lookback windows from trial #1 (lookback_fast=5, lookback_slow=4)

This is a CONSERVATIVE TUNING of trial #1:
- Slightly higher threshold (0.12 → 0.13) to reduce marginal trades
- But not as high as #31's 0.14 which killed the score
- Directional confirmation on slow (same sign) but not magnitude filtering
- Should yield ~30-32k trades (between #1's 35k and #31's 24k)
- Better quality than #1 while maintaining good trade frequency

The logic: Trial #1 works. Don't redesign it. Fine-tune the threshold to
reduce marginal signals without over-filtering. The sweet spot is probably
around 0.13.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.13,
        volatility_window: int = 20,
        size: int = 1,
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

        # Entry logic (tuned version of trial #1):
        # - Fast imbalance must exceed threshold (slightly raised to 0.13)
        # - Slow imbalance must be in same direction (confirming trend)
        # This maintains trial #1's selectivity while filtering marginal signals

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
