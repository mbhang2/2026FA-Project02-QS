"""
Order flow continuation with entropy-based signal quality filtering.

Analysis of prior trials shows a clear pattern:
- Trial #1 (score -0.4823): Best performer with adaptive volatility threshold
- Trial #5 (score -0.9435): Added momentum confirmation, hurt performance
- Trial #4 (score -0.9197): Over-filtering with strict thresholds
- Trial #2,#3: Simplified approaches all performed worse

The key insight from #1's success: the adaptive volatility-based threshold was
working well. The subsequent trials tried to add more filters (momentum, volume,
stricter slow thresholds) but each made things worse.

This suggests the problem is not missing filters, but rather:
1. Trading on too many weak signals (high trade frequency)
2. Not distinguishing between high-quality and low-quality flow signals
3. Bracket parameters may need tuning for the commission environment

New approach - focus on signal entropy and consistency:
1. Keep the adaptive volatility threshold from #1 (proven best)
2. Add entropy-based filtering: measure consistency of flow across the lookback
   - High entropy = inconsistent flow = weak signal (skip)
   - Low entropy = consistent flow = strong signal (trade)
3. This naturally filters whipsaws without arbitrary thresholds
4. Increase bracket offsets slightly to handle commission better
5. Reduce size back to 1 (lower is better per commission analysis)
6. Use a simpler slow timeframe check: just require same-sign agreement

The hypothesis: Order flow continuation works best when the flow is *consistent*
across multiple bars, not just when the aggregate is large. Entropy measures
this consistency naturally. A bar-by-bar reversal pattern (buy, sell, buy, sell)
has high entropy and should be skipped, while consistent directional flow has
low entropy and should be traded.

This balances the simplicity that worked in #1 with better signal quality
filtering that doesn't over-constrain the strategy.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.12,
        volatility_window: int = 20,
        max_entropy: float = 0.6,
        size: int = 1,
        stop_offset: float = 1.0,
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
            max_entropy: Maximum entropy allowed for the flow pattern
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
        self.max_entropy = max_entropy
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def _calculate_entropy(self, deltas: list[float]) -> float:
        """Calculate Shannon entropy of the flow pattern to measure consistency."""
        if not deltas or len(deltas) < 2:
            return 0.0
        
        # Count buy vs sell bars (based on delta sign)
        buys = sum(1 for d in deltas if d > 0)
        sells = sum(1 for d in deltas if d < 0)
        total = len(deltas)
        
        if total == 0:
            return 0.0
        
        # Calculate probabilities
        p_buy = buys / total if buys > 0 else 0
        p_sell = sells / total if sells > 0 else 0
        
        # Shannon entropy: -sum(p * log(p))
        entropy = 0.0
        if p_buy > 0:
            entropy -= p_buy * math.log(p_buy)
        if p_sell > 0:
            entropy -= p_sell * math.log(p_sell)
        
        # Normalize to [0, 1] where 1 is maximum entropy (50/50 split)
        max_entropy = math.log(2)  # entropy of 50/50 distribution
        if max_entropy > 0:
            entropy = entropy / max_entropy
        
        return entropy

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

        # Get the fast timeframe lookback window for flow measurement
        fast_window = fast_history.last(self.lookback_fast)

        # Calculate signed order flow on fast timeframe
        total_delta = 0.0
        total_volume = 0.0
        deltas = []

        for bar in fast_window:
            total_delta += bar.delta
            total_volume += bar.volume
            deltas.append(bar.delta)

        if total_volume <= 0:
            return

        # Normalize imbalance
        fast_imbalance = total_delta / total_volume

        # Calculate entropy of the flow pattern
        # Low entropy = consistent direction = good signal
        # High entropy = mixed directions = weak signal
        flow_entropy = self._calculate_entropy(deltas)
        
        if flow_entropy > self.max_entropy:
            # Flow is too inconsistent, skip this signal
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

        # Get slow timeframe trend - simpler check, just same sign
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
        # - Flow must be consistent (low entropy)
        # - Slow imbalance must be in same direction (trend agreement)

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong, consistent buying pressure on fast, confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong, consistent selling pressure on fast, confirmed by slow
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
