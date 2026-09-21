"""
Order flow continuation with adaptive confirmation strength and refined sizing.

Analysis of 43 trials reveals:

Best performers:
- Trial #42: score -0.4811 (refined cutoff 0.95, conservative scaling)
- Trial #41: score -0.4814 (original approach with volatility-responsive sizing)
- Trial #43: score -0.8599 (graduated sizing underperformed)

Key insight: Trial #42 (-0.4811) remains best. Trial #43's graduated sizing
formula (1 + (1 - vol_factor)) produced worse results, suggesting the binary
sizing approach is actually better tuned.

Current hypothesis: The issue with #43 was the graduated sizing formula itself.
The formula `size = max(1, int(round(1.0 + (1.0 - vol_factor))))` tends to
produce size=1 in most conditions, losing the benefit of higher sizing in low vol.

New hypothesis: CONFIRMATION STRENGTH ADAPTATION + REFINED BINARY SIZING

Keep the binary sizing from #42 (which works well) but improve confirmation:

1. Make the slow confirmation threshold adaptive based on volatility
   - In low vol: require stronger slow confirmation (harder to trigger)
   - In high vol: allow weaker slow confirmation (easier to trigger)
   - Rationale: In high vol, any slow signal is meaningful; in low vol, we need
     to filter noise more aggressively

2. Refine the slow confirmation formula:
   - Base threshold: 0.05 (from #43 attempt)
   - Adjust by volatility: threshold *= (1.0 / volatility_multiplier)
   - In low vol (vol_mult=0.85): threshold becomes 0.05 * (1.0/0.85) ≈ 0.059
   - In high vol (vol_mult=1.2): threshold becomes 0.05 * (1.0/1.2) ≈ 0.042
   - This inverts the logic: low vol requires higher absolute threshold

3. Keep the rest of #42:
   - Binary sizing: size=2 if vol_mult < 0.95, else size=1
   - Asymmetric thresholds: 0.10 for buy, 0.14 for sell
   - Volatility formula: 0.85 + 0.3 * volatility_ratio
   - Multi-timeframe: 1m fast, 15m slow

Why this works:
- Adaptive confirmation filters noise in low-vol periods (when false signals hurt)
- Maintains aggressive entry in high-vol periods (when signal is clearer)
- Binary sizing is proven to work well from #42
- The inversion (higher threshold in low vol) matches market intuition

Expected outcome: Score better than #42's -0.4811, potentially -0.4809 or better,
by using volatility-adaptive confirmation while keeping the proven sizing logic.
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
        base_confirmation_threshold: float = 0.05,
        volatility_window: int = 20,
        volatility_cutoff: float = 0.95,
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
            base_confirmation_threshold: Base slow imbalance threshold for confirmation
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
        self.base_confirmation_threshold = base_confirmation_threshold
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
            # Conservative volatility multiplier
            volatility_multiplier = 0.85 + 0.3 * volatility_ratio
        else:
            volatility_multiplier = 1.0

        # Determine position size based on volatility state (binary from #42)
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

        # Adaptive confirmation threshold based on volatility
        # In low vol: higher threshold (inverted scaling via 1/vol_mult)
        # In high vol: lower threshold (easier to confirm)
        # This filters noise in quiet periods while allowing signals in active periods
        if volatility_multiplier > 0:
            confirmation_threshold = self.base_confirmation_threshold / volatility_multiplier
        else:
            confirmation_threshold = self.base_confirmation_threshold

        # Entry logic with asymmetric thresholds and adaptive confirmation:
        # - Buy threshold is lower (0.10) to capture upside flow more readily
        # - Sell threshold is higher (0.14) to be more selective on shorts
        # - Slow imbalance confirmation strength adapts to volatility
        # - Position size is binary based on volatility state
        # - Thresholds adjusted conservatively for volatility

        buy_threshold_adjusted = self.buy_threshold * volatility_multiplier
        sell_threshold_adjusted = self.sell_threshold * volatility_multiplier

        if (fast_imbalance > buy_threshold_adjusted and 
            slow_imbalance > confirmation_threshold):
            # Strong buying pressure on fast, confirmed by adaptive slow signal
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif (fast_imbalance < -sell_threshold_adjusted and 
              slow_imbalance < -confirmation_threshold):
            # Strong selling pressure on fast, confirmed by adaptive slow signal
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
