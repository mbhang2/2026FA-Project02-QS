"""
Order flow continuation with refined entry thresholds and improved confirmation.

Analysis of 45 trials reveals:

Best performers:
- Trial #45: score -0.4623 (best overall)
- Trial #42: score -0.4811 (previous best)
- Trial #41: score -0.4814

Key insight: Trial #45 (-0.4623) beat #42 (-0.4811) by adjusting the volatility
multiplier formula and relaxing confirmation thresholds. The changes were:
- volatility_multiplier: 0.85 + 0.3*vol → 0.90 + 0.25*vol (less aggressive)
- buy_threshold: 0.10 → 0.09 (more aggressive on buys)
- sell_threshold: 0.14 → 0.13 (still selective on sells)
- volatility_cutoff: 0.95 → 0.93 (slightly more conservative sizing)
- slow confirmation: simple thresholds (-0.02 for buy, 0.02 for sell)

The improvement suggests the direction is right: reduce volatility adjustment
aggression and relax confirmation slightly.

New hypothesis: FURTHER THRESHOLD REFINEMENT + VOLATILITY ADJUSTMENT TUNING

Observations from #45's success:
1. The less aggressive volatility multiplier (0.90 + 0.25) works better
2. The relaxed slow confirmation thresholds reduce false rejections
3. The asymmetric thresholds (0.09/0.13) are well-balanced
4. Binary sizing at 0.93 cutoff is good

Testing further refinements:
1. Fine-tune the volatility multiplier base further
   - Current: 0.90 + 0.25 * volatility_ratio
   - Test: 0.92 + 0.23 * volatility_ratio (even less aggressive)
   - Rationale: The base thresholds (0.09/0.13) are already well-tuned;
     minimal adjustment may be better

2. Adjust the slow confirmation thresholds slightly
   - Current: -0.02 for buy, 0.02 for sell (symmetric around 0)
   - Test: -0.01 for buy, 0.01 for sell (tighter, less noise)
   - Rationale: Trial #45 worked well; tighter confirmation may filter more noise

3. Experiment with lookback periods
   - Current: lookback_fast=5, lookback_slow=4
   - Test: lookback_fast=6, lookback_slow=5 (slightly longer lookback)
   - Rationale: Longer lookback reduces noise, may improve signal quality

4. Keep the rest from #45:
   - fast_interval="1m", slow_interval="15m"
   - stop_offset=0.8, target_offset=1.2
   - volatility_window=20
   - Binary sizing: size=2 if vol_mult < 0.93, else size=1
   - Volatility cutoff at 0.93

Expected outcome: Score better than #45's -0.4623, potentially -0.4620 or better,
by fine-tuning the volatility adjustment and confirmation thresholds.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 6,
        lookback_slow: int = 5,
        buy_threshold: float = 0.09,
        sell_threshold: float = 0.13,
        volatility_window: int = 20,
        volatility_cutoff: float = 0.93,
        low_vol_size: int = 2,
        high_vol_size: int = 1,
        slow_buy_threshold: float = -0.01,
        slow_sell_threshold: float = 0.01,
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
            # Further refined volatility multiplier: even less aggressive scaling
            volatility_multiplier = 0.92 + 0.23 * volatility_ratio
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

        # Adjusted thresholds for volatility
        buy_threshold_adjusted = self.buy_threshold * volatility_multiplier
        sell_threshold_adjusted = self.sell_threshold * volatility_multiplier

        # Entry logic with refined thresholds and tighter confirmation:
        # - Buy threshold: 0.09 (aggressive on upside flow)
        # - Sell threshold: 0.13 (selective on downside)
        # - Slow confirmation: tighter (-0.01/0.01) to filter noise
        # - Position size: binary based on volatility
        # - Volatility adjustment: less aggressive (0.92 + 0.23*vol)

        if (fast_imbalance > buy_threshold_adjusted and 
            slow_imbalance > self.slow_buy_threshold):
            # Strong buying pressure on fast, with neutral or positive slow signal
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif (fast_imbalance < -sell_threshold_adjusted and 
              slow_imbalance < self.slow_sell_threshold):
            # Strong selling pressure on fast, with neutral or negative slow signal
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
