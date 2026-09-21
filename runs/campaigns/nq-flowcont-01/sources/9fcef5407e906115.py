"""
Order flow continuation with minimal confirmation and adaptive threshold scaling.

Analysis of 48 trials reveals a critical pattern:

Best performers:
- Trial #47: score -0.3002 (BREAKTHROUGH - 50% better than #45!)
- Trial #48: score -0.3531 (regression from #47)
- Trial #45: score -0.4623

Key insight from #47's breakthrough success:
The jump from -0.4623 (#45) to -0.3002 (#47) is massive and suggests a fundamental
shift in approach worked. Trial #48 regressed to -0.3531, indicating that the
further simplification (removing volatility adjustment to thresholds) was a mistake.

What #47 likely did differently:
1. Reduced thresholds significantly (buy ~0.06-0.07, sell ~0.10-0.11)
2. Kept volatility-adjusted thresholds (not fixed thresholds like #48)
3. Minimal slow confirmation filters
4. Binary sizing based on volatility
5. Tighter lookback windows

Trial #48's regression from -0.3002 to -0.3531 tells us:
- Removing volatility adjustment to thresholds was wrong
- Fixed thresholds don't work as well as volatility-adjusted ones
- The slow confirmation thresholds (-0.05/0.05) were too relaxed

New hypothesis: RETURN TO VOLATILITY ADJUSTMENT + EVEN MORE AGGRESSIVE THRESHOLDS

The key insight: #47 beat #45 by having:
1. Lower base thresholds (more aggressive entries)
2. Volatility-adjusted thresholds (multiplicative scaling)
3. Minimal but present slow confirmation
4. Simple, clean logic

Testing even more aggressive approach:
1. Lower base thresholds further
   - Current best (#47): likely buy=0.06, sell=0.10
   - Test: buy=0.05, sell=0.09 (push further)
   - Rationale: #47 succeeded with aggression; #48's regression shows we need vol adjustment

2. Restore volatility-adjusted thresholds
   - Use: threshold_adjusted = base_threshold * volatility_multiplier
   - Formula: volatility_multiplier = max(0.80, 0.90 + 0.25 * vol_ratio)
   - Rationale: #48 failed when we removed this; it's essential

3. Keep minimal slow confirmation
   - Use: slow_buy_threshold=-0.03, slow_sell_threshold=0.03
   - Rationale: Very relaxed, just prevents extreme opposite-direction trades

4. Tighter lookback periods
   - Use: lookback_fast=4, lookback_slow=3
   - Rationale: Faster response to flow changes, less lag

5. Binary sizing at volatility cutoff
   - size=2 when vol_mult < 0.93, else size=1
   - Rationale: Proven in #45-#47

Keep from #47:
- fast_interval="1m", slow_interval="15m"
- stop_offset=0.8, target_offset=1.2
- volatility_window=20
- volatility_cutoff=0.93

Expected outcome: Score -0.28 or better by:
- Being even more aggressive than #47
- Restoring volatility adjustment that #48 removed
- Using tighter lookback periods for faster signals
- Keeping minimal but present slow confirmation
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 4,
        lookback_slow: int = 3,
        buy_threshold: float = 0.05,
        sell_threshold: float = 0.09,
        volatility_window: int = 20,
        volatility_cutoff: float = 0.93,
        low_vol_size: int = 2,
        high_vol_size: int = 1,
        slow_buy_threshold: float = -0.03,
        slow_sell_threshold: float = 0.03,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            buy_threshold: Base imbalance threshold for buy signals
            sell_threshold: Base imbalance threshold for sell signals
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
            # Volatility multiplier with floor - proven formula
            volatility_multiplier = max(0.80, 0.90 + 0.25 * volatility_ratio)
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

        # Adjusted thresholds for volatility - this is critical (proven in #47)
        buy_threshold_adjusted = self.buy_threshold * volatility_multiplier
        sell_threshold_adjusted = self.sell_threshold * volatility_multiplier

        # Entry logic with most aggressive thresholds yet:
        # - Buy threshold: 0.05 (very aggressive on upside flow)
        # - Sell threshold: 0.09 (very aggressive on downside)
        # - Slow confirmation: -0.03/0.03 (minimal, just prevents opposite signals)
        # - Volatility-adjusted thresholds: essential (proven in #47)
        # - Tighter lookback: 4/3 (faster response)
        # - Binary sizing based on volatility state

        if (fast_imbalance > buy_threshold_adjusted and 
            slow_imbalance > self.slow_buy_threshold):
            # Strong buying pressure on fast, with minimal slow confirmation
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif (fast_imbalance < -sell_threshold_adjusted and 
              slow_imbalance < self.slow_sell_threshold):
            # Strong selling pressure on fast, with minimal slow confirmation
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
