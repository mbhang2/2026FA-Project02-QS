"""
Order flow continuation with refined threshold asymmetry and volatility-responsive sizing.

Analysis of 40 trials reveals a clear pattern:

Best performers:
- Trial #1: score -0.4823 (~35k trades) - baseline with adaptive volatility
- Trial #37: score -0.4820 (~36k trades) - asymmetric thresholds (buy=0.10, sell=0.14)
- Trial #40: score -0.4826 (~37k trades) - micro-refined thresholds (buy=0.09, sell=0.13)

Key insight: Trial #37 (-0.4820) remains the best. Trial #40's refinement to 0.09/0.13
actually regressed slightly to -0.4826, suggesting 0.10/0.14 is closer to optimal.

Failures analyzed:
- Trial #39 (-0.9408): Momentum regime detection was fundamentally misaligned
- Trial #38 (-0.4828): Session-aware brackets added complexity without benefit
- Multiple other attempts: All regressions or marginal changes

New hypothesis: THRESHOLD STABILITY + VOLATILITY-RESPONSIVE SIZING

Core insight: Trial #37's asymmetric thresholds (0.10/0.14) are optimal for entry.
But the session-time-based sizing (60 min cutoff, 1 vs 2 contracts) is crude.

Better approach: Size based on actual volatility state, not clock time.
- High volatility → smaller position (1 contract) to reduce risk
- Low volatility → normal position (2 contracts) to capture cleaner signals
- This is more adaptive than a fixed 60-minute cutoff
- Volatility is already computed for threshold adjustment; reuse it for sizing

Why this works:
1. Trial #37's thresholds (0.10/0.14) are proven optimal - don't change them
2. Volatility-responsive sizing is more principled than time-based sizing
3. High vol periods are noisier (similar to early session), so smaller size makes sense
4. Low vol periods have cleaner signals, so full size captures them better
5. This is a single parameter change: use vol_multiplier to also drive sizing
6. Avoids the complexity that killed #38 and #39

Implementation:
- Keep fast_interval="1m", slow_interval="15m", lookbacks 5/4
- Keep asymmetric thresholds at 0.10/0.14 (trial #37's winning values)
- Keep brackets at 0.8/1.2
- CHANGE: Replace time-based sizing with volatility-based sizing
  - If vol_multiplier < 0.95: size = 1 (low volatility, normal thresholds)
  - If vol_multiplier >= 0.95: size = 2 (high volatility, need more conviction)
  - Wait, that's backwards. High vol means noisier, so we want SMALLER size
  - If vol_multiplier < 1.0: size = 2 (low volatility, cleaner signals)
  - If vol_multiplier >= 1.0: size = 1 (high volatility, noisier signals)
- Keep volatility multiplier formula at 0.8 + 0.4 * vol_ratio (trial #37's version)

Expected outcome: Slightly better than #37's -0.4820 by using volatility-responsive
sizing instead of time-based sizing. More stable than attempts to add new regime logic.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        buy_threshold: float = 0.10,
        sell_threshold: float = 0.14,
        volatility_window: int = 20,
        volatility_cutoff: float = 1.0,
        low_vol_size: int = 2,
        high_vol_size: int = 1,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            buy_threshold: Imbalance threshold for buy signals
            sell_threshold: Imbalance threshold for sell signals
            volatility_window: Bars to measure volatility over
            volatility_cutoff: Threshold for volatility-based sizing decision
            low_vol_size: Position size when volatility is low
            high_vol_size: Position size when volatility is high
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.volatility_window = volatility_window
        self.volatility_cutoff = volatility_cutoff
        self.low_vol_size = low_vol_size
        self.high_vol_size = high_vol_size
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
            # Volatility multiplier: higher volatility = higher multiplier
            volatility_multiplier = 0.8 + 0.4 * volatility_ratio
        else:
            volatility_multiplier = 1.0

        # Determine position size based on volatility state
        # High volatility (multiplier >= cutoff) = smaller size (noisier signals)
        # Low volatility (multiplier < cutoff) = normal size (cleaner signals)
        if volatility_multiplier < self.volatility_cutoff:
            size = self.low_vol_size
        else:
            size = self.high_vol_size

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

        # Entry logic with asymmetric thresholds:
        # - Buy threshold is lower (0.10) to capture upside flow more readily
        # - Sell threshold is higher (0.14) to be more selective on shorts
        # - Slow imbalance provides directional confirmation
        # - Position size varies by volatility state

        buy_threshold_adjusted = self.buy_threshold * volatility_multiplier
        sell_threshold_adjusted = self.sell_threshold * volatility_multiplier

        if fast_imbalance > buy_threshold_adjusted and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -sell_threshold_adjusted and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
