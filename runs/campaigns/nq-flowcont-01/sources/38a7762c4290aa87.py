"""
Order flow continuation with aggressive threshold tuning and volatility-driven sizing.

Analysis of 46 trials reveals clear patterns:

Best performers:
- Trial #45: score -0.4623 (best so far)
- Trial #42: score -0.4811 (previous best)
- Trial #41: score -0.4814

Key insight from #45's success: The volatility multiplier 0.90 + 0.25*vol works
better than more aggressive formulas. The relaxed slow confirmation (-0.02/0.02)
reduces false rejections effectively.

Trial #46 tested tighter confirmation (-0.01/0.01) and longer lookbacks (6/5) but
only achieved -0.4860, suggesting the tighter confirmation was too strict.

New hypothesis: AGGRESSIVE THRESHOLD REDUCTION + VOLATILITY FLOOR

Observations:
1. The base thresholds (0.09/0.13) work well but may still be too high
2. The volatility multiplier 0.90 + 0.25*vol is good; don't over-adjust
3. The slow confirmation at -0.02/0.02 is better than -0.01/0.01
4. Longer lookbacks (6/5) didn't help; revert to (5/4)
5. The core signal (fast flow imbalance) is strong; reduce entry barriers

Testing more aggressive thresholds:
1. Lower the base thresholds further
   - Current: buy=0.09, sell=0.13
   - Test: buy=0.08, sell=0.12 (more aggressive entries)
   - Rationale: Lower barriers let us capture more flow signals that work

2. Keep the volatility multiplier that works
   - Stay with: 0.90 + 0.25 * volatility_ratio
   - This formula has proven effective in #45

3. Revert slow confirmation to the working values
   - Use: slow_buy_threshold=-0.02, slow_sell_threshold=0.02
   - These reduced false rejections in #45

4. Revert lookback periods to the shorter, faster version
   - Use: lookback_fast=5, lookback_slow=4
   - These were good in #42/#45

5. Add a volatility floor to prevent over-aggressive behavior
   - When volatility_multiplier < 0.85, cap it at 0.85
   - This prevents thresholds from becoming too loose in very low vol
   - Rationale: In extremely calm markets, we don't want to over-trade

Keep from #45:
- fast_interval="1m", slow_interval="15m"
- stop_offset=0.8, target_offset=1.2
- volatility_window=20
- Binary sizing: size=2 if vol_mult < 0.93, else size=1
- volatility_cutoff=0.93

Changes from #45:
- buy_threshold: 0.09 → 0.08 (more aggressive)
- sell_threshold: 0.13 → 0.12 (more aggressive)
- lookback_fast: 6 → 5 (revert to faster)
- lookback_slow: 5 → 4 (revert to faster)
- slow_buy_threshold: -0.02 (keep)
- slow_sell_threshold: 0.02 (keep)
- Add volatility floor: min(volatility_multiplier, 0.85) → volatility_multiplier

Expected outcome: Score better than #45's -0.4623, potentially -0.4618 or better,
by reducing entry barriers while maintaining the proven volatility adjustment.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        buy_threshold: float = 0.08,
        sell_threshold: float = 0.12,
        volatility_window: int = 20,
        volatility_cutoff: float = 0.93,
        low_vol_size: int = 2,
        high_vol_size: int = 1,
        slow_buy_threshold: float = -0.02,
        slow_sell_threshold: float = 0.02,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
        volatility_floor: float = 0.85,
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
            volatility_floor: Minimum volatility multiplier to prevent over-trading
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
        self.volatility_floor = volatility_floor

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
            # Use proven volatility multiplier formula with floor
            volatility_multiplier = max(self.volatility_floor, 0.90 + 0.25 * volatility_ratio)
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

        # Entry logic with aggressive thresholds and proven confirmation:
        # - Buy threshold: 0.08 (more aggressive on upside flow)
        # - Sell threshold: 0.12 (more aggressive on downside)
        # - Slow confirmation: -0.02/0.02 (proven to work)
        # - Position size: binary based on volatility
        # - Volatility adjustment: proven formula with floor
        # - Faster lookback periods: 5/4 (proven in #42/#45)

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
