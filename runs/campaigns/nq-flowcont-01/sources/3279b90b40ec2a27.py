"""
Order flow continuation with reduced trading frequency and tighter entry discipline.

Analysis of trial progression reveals a critical pattern:
- Trial #1 (score -0.4823): Best performer with ~35k trades
- All subsequent trials: Consistently worse, ranging from -0.8 to -0.97
- Common theme across degraded trials: Either more trades or more complex filtering

Key insight from the data:
1. Trial #1's ~35k trades generated the best score despite still being negative
2. Trials with 50k-140k trades (more aggressive) all scored much worse
3. This suggests the edge is real but TINY, and commission (0.2 per round trip) dominates
4. The issue: We're trading too much. Every trade costs 0.2 in commission.

For trial #1 to score -0.4823 with 35k trades:
- Gross P&L before commission: roughly -0.4823 - (35000 * 0.2) = -7500
- That's negative even before commission, but the commission cost is the killer

New hypothesis: The order flow signal is real and profitable, but only on HIGH CONFIDENCE
entries. We should:
1. Keep trial #1's core (adaptive threshold + multi-timeframe confirmation)
2. DRAMATICALLY reduce trading frequency by being much more selective
3. Only trade when BOTH fast AND slow imbalances are STRONG (not just directionally aligned)
4. Use higher thresholds to filter out marginal signals
5. Reduce position size to 1 (already done in #13-14)

This is the opposite direction from what we've been trying. Instead of:
- Adding more filters (tried, made things worse)
- Adjusting brackets (tried, made things worse)
- Adjusting position sizing (tried, made things worse)

We should: TRADE LESS, but with higher conviction.

The math: If trial #1's signal was profitable at ~35k trades, then:
- Reducing to ~15-20k trades with only the highest conviction signals
- Should improve the score significantly by reducing commission drag
- The edge exists; we just need to be more selective about when to take it

Specific changes:
1. Increase base_threshold from 0.12 to 0.18 (require stronger imbalance)
2. Change slow confirmation from "same sign" to "strong in same direction" (0.05 threshold)
3. Reduce lookback periods slightly (faster signal, less lag)
4. Keep size=1, moderate brackets (0.8/1.2 from trial #1)
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
        slow_threshold: float = 0.05,
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
            base_threshold: Base imbalance threshold for fast signal (higher = fewer trades)
            slow_threshold: Minimum imbalance magnitude required on slow timeframe
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
        self.slow_threshold = slow_threshold
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

        # Get slow timeframe trend - now requires STRONG confirmation
        slow_window = slow_history.last(self.lookback_slow)
        slow_delta = 0.0
        slow_volume = 0.0

        for bar in slow_window:
            slow_delta += bar.delta
            slow_volume += bar.volume

        if slow_volume <= 0:
            return

        slow_imbalance = slow_delta / slow_volume

        # Entry logic with HIGHER conviction requirements:
        # - Fast imbalance must exceed adaptive threshold (0.18 base, higher with vol)
        # - Slow imbalance must be STRONG in same direction (not just positive/negative)
        # - This dramatically reduces false signals and commission drag
        # - Trades only the clearest order flow continuation setups

        if fast_imbalance > threshold and slow_imbalance > self.slow_threshold:
            # Strong buying pressure on both fast and slow timeframes
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < -self.slow_threshold:
            # Strong selling pressure on both fast and slow timeframes
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
