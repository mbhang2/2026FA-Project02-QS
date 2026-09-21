"""
Order flow continuation with volatility-adaptive entry and exit management.

Analysis of trial history shows:
- Trial #1 (best): score -0.4823, ~35k trades - adaptive threshold + multi-timeframe
- Trial #22: score -0.8647, ~31k trades - session timing filter hurt performance
- Trial #21: score -0.8647, ~31k trades - raising threshold to 0.18 hurt performance
- Trial #20: score -0.8909, ~34k trades - adding filters made things worse

Key insight from trial #1's success: The core signal (adaptive threshold + multi-timeframe
confirmation) works. Every subsequent attempt to "improve" it through additional filters
(session timing, higher thresholds, reversal checks) made things worse.

This suggests the signal is real but the improvements were over-constrained. Trial #1
traded 35k times with score -0.4823. The negative score indicates we're losing money
overall, but the signal itself is the best we've found.

New approach - FOCUS ON EXIT QUALITY:
The problem isn't the entries; it's the exits. We're using fixed brackets (stop_offset=0.8,
target_offset=1.2) on every trade. But order flow continuation has variable strength:
- Some flow signals are very strong (large delta, high imbalance)
- Some are weak (barely above threshold)

Hypothesis: Vary the bracket sizes based on signal strength.
1. Keep trial #1's core: adaptive volatility threshold (0.12) + multi-timeframe confirmation
2. Keep lookback_fast=5, lookback_slow=4 (trial #1 parameters)
3. Keep base_threshold=0.12 (trial #1 value)
4. VARY THE BRACKET based on signal strength:
   - Measure how far fast_imbalance exceeds the threshold
   - Measure how strong slow_imbalance is
   - Use these to scale stop_offset and target_offset
   - Strong signals: tighter stops (0.6), wider targets (1.5)
   - Weak signals: wider stops (1.0), narrower targets (0.8)
5. Keep size=1 (let bracket scaling handle risk management)

The math: If the signal strength predicts exit quality, then adapting bracket sizes
to signal strength should improve win rate and reduce losses on marginal trades.
Trial #1 scored -0.4823. If we can shift the distribution of outcomes by using
stronger brackets for strong signals and looser brackets for weak signals, we might
improve to -0.35 to -0.40 range.

This isn't adding complexity at the signal level (which made things worse), it's
improving the risk management around the same signal (which is a different lever).
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.12,
        volatility_window: int = 20,
        size: int = 1,
        base_stop_offset: float = 0.8,
        base_target_offset: float = 1.2,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Base imbalance threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            size: Position size
            base_stop_offset: Base stop loss distance (scaled by signal strength)
            base_target_offset: Base profit target distance (scaled by signal strength)
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.size = size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset

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
            # Adapt threshold: higher volatility = higher threshold needed
            threshold = self.base_threshold * (0.8 + 0.4 * volatility_ratio)
        else:
            threshold = self.base_threshold

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

        # Entry logic: same as trial #1
        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            # Scale bracket based on signal strength
            signal_strength = min(fast_imbalance / (threshold * 2.0), 1.0)  # 0 to 1
            
            # Strong signals get tighter stops, wider targets
            # Weak signals (just above threshold) get looser stops, tighter targets
            stop_offset = self.base_stop_offset * (1.2 - 0.4 * signal_strength)
            target_offset = self.base_target_offset * (0.7 + 0.6 * signal_strength)
            
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            # Scale bracket based on signal strength
            signal_strength = min(abs(fast_imbalance) / (threshold * 2.0), 1.0)  # 0 to 1
            
            # Strong signals get tighter stops, wider targets
            # Weak signals (just above threshold) get looser stops, tighter targets
            stop_offset = self.base_stop_offset * (1.2 - 0.4 * signal_strength)
            target_offset = self.base_target_offset * (0.7 + 0.6 * signal_strength)
            
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
