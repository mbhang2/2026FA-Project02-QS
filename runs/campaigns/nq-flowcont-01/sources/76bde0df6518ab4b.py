"""
Order flow continuation with refined threshold asymmetry and enhanced confirmation.

Analysis of 41 trials shows:

Best performers:
- Trial #37: score -0.4820 (asymmetric thresholds 0.10/0.14, ~36k trades)
- Trial #41: score -0.4814 (volatility-responsive sizing, ~36k trades)
- Trial #1: score -0.4823 (baseline, ~35k trades)

Key observation: Trial #41 (-0.4814) is the current best. It uses:
1. Asymmetric thresholds (0.10/0.14) from trial #37
2. Volatility-responsive sizing (smaller in high vol, normal in low vol)
3. Multi-timeframe confirmation (1m + 15m)

The volatility-responsive sizing approach in #41 is sound but the cutoff at 1.0
may be too simplistic. Current logic:
- If vol_multiplier < 1.0: size = 2 (low vol → normal size)
- If vol_multiplier >= 1.0: size = 1 (high vol → smaller size)

New hypothesis: REFINED VOLATILITY CUTOFF + THRESHOLD ADAPTATION

The issue with #41: The volatility multiplier ranges roughly 0.8-1.2 in normal
conditions. A hard cutoff at 1.0 means we're at size=2 for vol_multiplier in
[0.8, 1.0) and size=1 for [1.0, 1.2+). This is a narrow band.

Better approach: Use a more nuanced cutoff point that better separates quiet
vs noisy periods. Testing suggests 0.95 might be better:
- If vol_multiplier < 0.95: size = 2 (genuinely low volatility)
- If vol_multiplier >= 0.95: size = 1 (elevated volatility)

This keeps more trades at full size during normal-to-low vol, and only reduces
size when vol truly elevated. This matches the intuition that most of the day
is "normal" and we want full exposure to the signal.

Additionally, refine the threshold adjustment formula:
- Current: volatility_multiplier = 0.8 + 0.4 * volatility_ratio
- This means thresholds get multiplied by vol_multiplier
- In low vol (0.8-0.95): thresholds reduced by 15-20%, making signals easier
- In high vol (1.0-1.2+): thresholds increased by 0-20%, making signals harder

This is correct in direction but the magnitude might be off. Test slightly
different scaling to see if thresholds should adjust less aggressively.

New formula: volatility_multiplier = 0.85 + 0.3 * volatility_ratio
- More conservative scaling (0.3 instead of 0.4)
- Slightly higher floor (0.85 instead of 0.8)
- Result: Thresholds stay closer to their base values
- Rationale: The base thresholds (0.10/0.14) are already well-tuned; don't
  adjust them too much for volatility

Keep everything else from #41:
- fast_interval="1m", slow_interval="15m"
- lookback_fast=5, lookback_slow=4
- buy_threshold=0.10, sell_threshold=0.14
- stop_offset=0.8, target_offset=1.2
- volatility_window=20

Expected outcome: Score better than #41's -0.4814, potentially -0.4810 or better,
by using a more refined volatility cutoff (0.95 vs 1.0) and less aggressive
threshold scaling. This maintains the core insight while fine-tuning the mechanics.
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
            # Refined volatility multiplier: more conservative scaling
            volatility_multiplier = 0.85 + 0.3 * volatility_ratio
        else:
            volatility_multiplier = 1.0

        # Determine position size based on volatility state
        # Use refined cutoff at 0.95 instead of 1.0
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
        # - Thresholds adjusted less aggressively for volatility

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
