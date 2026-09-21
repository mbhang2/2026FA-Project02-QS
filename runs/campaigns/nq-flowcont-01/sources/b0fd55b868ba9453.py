"""
Order flow continuation with dynamic threshold scaling and session-aware confirmation.

Analysis of 44 trials reveals:

Best performers:
- Trial #42: score -0.4811 (refined cutoff 0.95, conservative scaling 0.85+0.3*vol)
- Trial #41: score -0.4814 (original approach with volatility-responsive sizing)
- Trial #44: score -0.5069 (adaptive confirmation underperformed)

Key insight: Trial #42 (-0.4811) remains best. The binary sizing with refined
volatility cutoff at 0.95 and conservative scaling works well.

However, analysis of the winning strategies suggests room for improvement:
1. The thresholds (0.10/0.14) are asymmetric but static
2. The slow confirmation threshold may be too strict
3. The volatility scaling formula (0.85 + 0.3 * ratio) may not capture all dynamics

New hypothesis: THRESHOLD ASYMMETRY OPTIMIZATION + SESSION-AWARE CONFIRMATION

Key observations from best trials:
- Asymmetric thresholds (0.10 buy vs 0.14 sell) work well
- But the exact ratio (0.10/0.14 ≈ 0.71) may be suboptimal
- The slow confirmation in #42 was simple (slow_imbalance > 0)
- This simplicity may be better than complex adaptive logic

Testing new parameters:
1. Adjust asymmetric thresholds to test different ratios
   - Current: buy=0.10, sell=0.14 (ratio 0.71)
   - Test: buy=0.09, sell=0.13 (ratio 0.69) - slightly more aggressive on buy
   - Rationale: Buy signals are more valuable if they have lower false rate

2. Refine volatility adjustment to be less aggressive
   - Current formula: 0.85 + 0.3 * volatility_ratio
   - Test: 0.90 + 0.25 * volatility_ratio (less extreme adjustment)
   - Rationale: Base thresholds are well-tuned; don't adjust too much

3. Keep slow confirmation simple but slightly relaxed
   - Instead of slow_imbalance > 0, use slow_imbalance > -0.02
   - This allows weak opposing slow signals (reduces false rejections)
   - Rationale: Fast signal is primary; slow just needs to not strongly contradict

4. Adjust volatility cutoff slightly
   - Current: 0.95
   - Test: 0.93 (slightly more conservative sizing in higher vol)
   - Rationale: May reduce losses in choppy periods

Keep from #42:
- fast_interval="1m", slow_interval="15m"
- lookback_fast=5, lookback_slow=4
- stop_offset=0.8, target_offset=1.2
- volatility_window=20
- Binary sizing: size=2 in low vol, size=1 in high vol

Changes:
- buy_threshold: 0.10 → 0.09
- sell_threshold: 0.14 → 0.13
- volatility_multiplier: 0.85 + 0.3*vol → 0.90 + 0.25*vol
- volatility_cutoff: 0.95 → 0.93
- slow_imbalance confirmation: > 0 → > -0.02 for buys, < 0 → < 0.02 for sells

Expected outcome: Score better than #42's -0.4811, potentially -0.4808 or better,
by fine-tuning the threshold asymmetry and relaxing the slow confirmation slightly.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        buy_threshold: float = 0.09,
        sell_threshold: float = 0.13,
        volatility_window: int = 20,
        volatility_cutoff: float = 0.93,
        low_vol_size: int = 2,
        high_vol_size: int = 1,
        slow_buy_threshold: float = -0.02,
        slow_sell_threshold: float = 0.02,
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
            # Refined volatility multiplier: less aggressive scaling
            volatility_multiplier = 0.90 + 0.25 * volatility_ratio
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

        # Entry logic with fine-tuned asymmetric thresholds:
        # - Buy threshold is lower (0.09) to capture upside flow more readily
        # - Sell threshold is higher (0.13) to be more selective on shorts
        # - Slow confirmation is relaxed slightly to reduce false rejections
        # - Position size varies by volatility state
        # - Thresholds adjusted less aggressively for volatility

        if (fast_imbalance > buy_threshold_adjusted and 
            slow_imbalance > self.slow_buy_threshold):
            # Strong buying pressure on fast, with weak or neutral slow signal
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif (fast_imbalance < -sell_threshold_adjusted and 
              slow_imbalance < self.slow_sell_threshold):
            # Strong selling pressure on fast, with weak or neutral slow signal
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
