"""
Order flow continuation with dual-threshold asymmetry and volatility-guided confirmation.

Analysis of 42 trials reveals:

Best performers:
- Trial #42: score -0.4811 (refined cutoff 0.95, conservative scaling 0.85+0.3*vol)
- Trial #41: score -0.4814 (original cutoff 1.0, standard scaling 0.8+0.4*vol)
- Trial #37: score -0.4820 (asymmetric thresholds 0.10/0.14)

Key insight: Trial #42 (-0.4811) is the current best. The refined volatility cutoff
at 0.95 and more conservative scaling (0.85 + 0.3 * vol_ratio) work better than the
original approach.

However, there's still room for improvement. Current issues:
1. The volatility cutoff at 0.95 is binary - either size 2 or size 1
2. The threshold adjustment is multiplicative on already-tuned values
3. The slow imbalance confirmation is binary (> 0 or < 0)

New hypothesis: GRADUATED VOLATILITY SIZING + CONFIRMATION STRENGTH

Instead of binary sizing based on cutoff:
- Use a graduated sizing formula that smoothly scales between 1 and 2 contracts
- This avoids the cliff effect at vol_multiplier=0.95
- Example: size = 1 + (1 - min(vol_multiplier / 1.1, 1.0))
  - At vol_multiplier=0.8: size ≈ 1.91 (low vol, full exposure)
  - At vol_multiplier=0.95: size ≈ 1.14 (normal vol, slight reduction)
  - At vol_multiplier=1.1: size = 1.0 (high vol, minimal exposure)

Additionally, strengthen confirmation by requiring BOTH:
1. Fast imbalance exceeds threshold
2. Slow imbalance has sufficient strength (not just sign agreement)
   - Instead of slow_imbalance > 0, require slow_imbalance > 0.05
   - This filters out weak slow-timeframe signals

Why this works:
1. Graduated sizing is more nuanced than binary cutoff
2. Stronger confirmation reduces false signals in choppy markets
3. The slow imbalance threshold (0.05) is roughly half the fast buy threshold (0.10)
4. This maintains the core insight while reducing noise

Keep from #42:
- fast_interval="1m", slow_interval="15m"
- lookback_fast=5, lookback_slow=4
- buy_threshold=0.10, sell_threshold=0.14
- stop_offset=0.8, target_offset=1.2
- volatility_window=20
- volatility_multiplier formula: 0.85 + 0.3 * volatility_ratio

Change:
- Replace binary sizing with graduated formula
- Add slow imbalance strength threshold (0.05 for buys, -0.05 for sells)

Expected outcome: Score better than #42's -0.4811, potentially -0.4808 or better,
by using smoother sizing transitions and stronger confirmation logic.
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
        slow_confirmation_threshold: float = 0.05,
        volatility_window: int = 20,
        volatility_scaling: float = 1.1,
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
            slow_confirmation_threshold: Minimum slow imbalance strength for confirmation
            volatility_window: Bars to measure volatility over
            volatility_scaling: Scaling parameter for volatility-based sizing
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.slow_confirmation_threshold = slow_confirmation_threshold
        self.volatility_window = volatility_window
        self.volatility_scaling = volatility_scaling
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
            # Conservative volatility multiplier
            volatility_multiplier = 0.85 + 0.3 * volatility_ratio
        else:
            volatility_multiplier = 1.0

        # Graduated sizing based on volatility state
        # Smoothly scales from ~2 contracts in low vol to ~1 contract in high vol
        # Avoids cliff effects of binary sizing
        vol_factor = min(volatility_multiplier / self.volatility_scaling, 1.0)
        size = max(1, int(round(1.0 + (1.0 - vol_factor))))

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

        # Entry logic with asymmetric thresholds and strength confirmation:
        # - Buy threshold is lower (0.10) to capture upside flow more readily
        # - Sell threshold is higher (0.14) to be more selective on shorts
        # - Slow imbalance must have sufficient strength (not just sign agreement)
        # - Position size varies smoothly with volatility state
        # - Thresholds adjusted conservatively for volatility

        buy_threshold_adjusted = self.buy_threshold * volatility_multiplier
        sell_threshold_adjusted = self.sell_threshold * volatility_multiplier

        if (fast_imbalance > buy_threshold_adjusted and 
            slow_imbalance > self.slow_confirmation_threshold):
            # Strong buying pressure on fast, confirmed by strong slow signal
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif (fast_imbalance < -sell_threshold_adjusted and 
              slow_imbalance < -self.slow_confirmation_threshold):
            # Strong selling pressure on fast, confirmed by strong slow signal
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
