"""
Order flow continuation with asymmetric threshold tuning and volatility optimization.

Analysis of 47 trials reveals:

Best performers:
- Trial #47: score -0.3002 (BEST - significant improvement!)
- Trial #45: score -0.4623
- Trial #42: score -0.4811

Key insight from #47's breakthrough: A much simpler approach works better.
Trial #47 achieved -0.3002 by using minimal parameters and direct flow signals.

Hypothesis: The previous trials were over-optimized with too many tuning parameters.
Trial #47 suggests that:
1. Simpler is better - fewer confirmation filters
2. The core signal (fast flow imbalance) is very strong
3. Volatility adjustment may have been counterproductive
4. Aggressive entry thresholds work better than conservative ones

New approach: SIMPLIFICATION + ASYMMETRIC THRESHOLDS + DIRECT FLOW SIGNALS

Observations from #47's success:
1. Fewer parameters = better generalization
2. Direct flow signals without heavy confirmation work well
3. The asymmetry between buy (0.08) and sell (0.12) is good
4. Binary sizing based on volatility state is effective
5. Bracket protection (stop_offset=0.8, target_offset=1.2) is solid

Testing further simplification:
1. Remove or reduce slow timeframe confirmation
   - Current #47 approach: direct flow signals
   - Test: minimal slow confirmation, only on extreme cases
   - Rationale: #47 showed simple works; don't over-filter

2. Adjust thresholds based on #47's success pattern
   - Current: buy=0.08, sell=0.12
   - Test: buy=0.07, sell=0.11 (even more aggressive, like #47)
   - Rationale: #47 beat #45 significantly; push further in that direction

3. Simplify volatility adjustment
   - Current: complex multiplier formula
   - Test: simpler volatility check, just for sizing decision
   - Rationale: #47 may have used minimal volatility adjustment

4. Keep what works:
   - fast_interval="1m", slow_interval="15m"
   - stop_offset=0.8, target_offset=1.2
   - Binary sizing at volatility cutoff
   - Lookback periods: 5/4 (fast/slow)

5. Reduce slow confirmation thresholds to minimal levels
   - Use: slow_buy_threshold=-0.05, slow_sell_threshold=0.05
   - Rationale: Very relaxed, almost no filter, let the fast signal dominate

Expected outcome: Score better than #47's -0.3002 by fine-tuning the simplified
approach. Target: -0.2900 or better.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        buy_threshold: float = 0.07,
        sell_threshold: float = 0.11,
        volatility_window: int = 20,
        volatility_cutoff: float = 0.93,
        low_vol_size: int = 2,
        high_vol_size: int = 1,
        slow_buy_threshold: float = -0.05,
        slow_sell_threshold: float = 0.05,
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
            slow_buy_threshold: Minimum slow imbalance for buy confirmation
            slow_sell_threshold: Maximum slow imbalance for sell confirmation
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
        self.slow_buy_threshold = slow_buy_threshold
        self.slow_sell_threshold = slow_sell_threshold
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

        # Calculate volatility for sizing decision only (simplified)
        volatility_lookback = min(self.volatility_window, len(fast_history))
        if volatility_lookback > 0:
            vol_window = fast_history.last(volatility_lookback)
            price_range = max(b.high for b in vol_window) - min(b.low for b in vol_window)
            avg_price = sum(b.close for b in vol_window) / len(vol_window)
            if avg_price > 0:
                volatility_ratio = price_range / avg_price
            else:
                volatility_ratio = 1.0
            # Simplified volatility multiplier - linear, less aggressive
            volatility_multiplier = 0.85 + 0.15 * volatility_ratio
        else:
            volatility_multiplier = 1.0

        # Determine position size based on volatility state
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

        # NO volatility adjustment to thresholds - use fixed thresholds
        # This simplification may be key to #47's success

        # Entry logic with simplified approach:
        # - More aggressive thresholds (0.07/0.11)
        # - Minimal slow confirmation (very relaxed)
        # - Let the fast flow signal dominate
        # - Fixed thresholds, not volatility-adjusted

        if (fast_imbalance > self.buy_threshold and 
            slow_imbalance > self.slow_buy_threshold):
            # Strong buying pressure on fast, with relaxed slow confirmation
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif (fast_imbalance < -self.sell_threshold and 
              slow_imbalance < self.slow_sell_threshold):
            # Strong selling pressure on fast, with relaxed slow confirmation
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
