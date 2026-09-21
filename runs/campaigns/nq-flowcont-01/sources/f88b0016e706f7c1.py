"""
Order flow continuation with strict signal confirmation and reduced false positives.

Analysis of trial history reveals:
- Trial #1 (best): score -0.4823, ~35k trades - adaptive threshold + multi-timeframe
- Trial #20: score -0.8909, ~34k trades - added reversal/volume filters but worse
- Trial #19: score -0.9599, ~106k trades - session phasing made it worse
- Trial #18: score -0.8289, ~30k trades - trade frequency limiting helped slightly

Key insight: Trial #1 was simple and worked. Every subsequent attempt to "improve"
the signal through additional filters or regime awareness made things worse.

The pattern suggests:
1. The order flow continuation signal is REAL but WEAK
2. Adding complexity doesn't improve it - it adds false negatives that hurt more than false positives
3. The best approach is to trust the signal but make it HARDER to trigger (higher bar, not more filters)

New approach - RAISE THE BAR, not ADD FILTERS:
1. Keep the adaptive volatility threshold from trial #1 (it worked)
2. Keep multi-timeframe confirmation from trial #1 (it worked)
3. INCREASE the base threshold from 0.12 to 0.18-0.20
   - This reduces trade count but keeps only the strongest signals
   - Trial #1 traded 35k times; reducing by 30-40% while keeping winners is the goal
4. REQUIRE stronger slow confirmation: slow_imbalance must exceed 0.05 (not just be positive)
   - This filters out weak confirmations where slow is barely positive
5. Reduce lookback periods slightly (4 bars instead of 5 for fast, 3 instead of 4 for slow)
   - Faster reaction to fresh flow data, reduces stale signal trades
6. Keep size=1, stop_offset=0.8, target_offset=1.2 (trial #1 parameters that worked)
7. DO NOT add reversal checks, volume filters, or session phasing
   - These all made things worse in trials #19, #20

The math: Trial #1 scored -0.4823 on 35k trades.
If we can reduce trade count by 35% (to ~23k) by raising the threshold,
but keep 95% of the winners (only lose the marginal trades that were losing anyway),
we might improve to -0.35 to -0.40 range.

The hypothesis: Trial #1 found the signal. The signal is weak but real.
Don't over-engineer it. Just make it harder to trigger so we only trade the best setups.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 4,
        lookback_slow: int = 3,
        base_threshold: float = 0.18,
        volatility_window: int = 20,
        slow_threshold: float = 0.05,
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
            slow_threshold: Minimum slow imbalance magnitude required
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
        self.slow_threshold = slow_threshold
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
        # - Fast imbalance must exceed adaptive threshold (raised to 0.18-0.20)
        # - Slow imbalance must be in same direction AND exceed slow_threshold (0.05)
        # - This creates a higher bar: both conditions must be met strongly
        # - Fewer trades, but higher quality signals

        if fast_imbalance > threshold and slow_imbalance > self.slow_threshold:
            # Strong buying pressure on fast, strongly confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < -self.slow_threshold:
            # Strong selling pressure on fast, strongly confirmed by slow
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
