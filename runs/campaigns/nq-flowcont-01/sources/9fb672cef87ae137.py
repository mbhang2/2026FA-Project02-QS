"""
Order flow continuation with flow reversal detection and adaptive entry timing.

Analysis of trial progression reveals:
- Trial #1 (score -0.4823): Best performer with ~35k trades
- Trial #10 (score -0.9554): Session-aware filtering added but degraded performance
- All other trials: Worse than #1, ranging -0.85 to -0.97

The pattern is clear: Trial #1's success came from:
1. Adaptive volatility threshold (multiplier 0.8 + 0.4 * volatility_ratio)
2. Multi-timeframe confirmation (fast signal + slow trend agreement)
3. Moderate trading frequency that balanced signal quality with frequency

The degradation in #10 suggests session-close filtering was too restrictive.

New hypothesis: The issue isn't frequency or session timing, but signal quality.
Trial #1 traded ~35k times; trial #2 traded 2.3x more and scored worse. This suggests
we need to be MORE selective about which signals we take, not less.

Key insight: Not all order flow signals are equal. A signal that just crossed the
threshold is fragile; a signal that is strongly established is more robust. Also,
flow that is reversing (recent bars show opposite imbalance) suggests the move is
ending, not continuing.

Refined approach:
1. Keep trial #1's proven adaptive volatility threshold
2. Keep multi-timeframe confirmation (fast + slow agreement)
3. Add flow reversal detection: check if the last bar's imbalance contradicts the
   lookback period's imbalance. If so, skip entry (flow is reversing).
4. Add signal persistence: require the signal to be consistent across the lookback
   window, not just on average. If individual bars show conflicting signals, skip.
5. This should reduce false signals while maintaining the core edge.

The hypothesis: Order flow continuation works, but we should only trade when flow
is not reversing and is consistently directional. This increases selectivity without
changing the core mechanism or adding complex signal strength logic.
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
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
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
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def _is_flow_reversing(self, window, threshold: float) -> bool:
        """
        Check if flow is reversing. If the most recent bar contradicts the
        overall window direction, the flow is likely reversing.
        
        Returns True if we should skip this signal.
        """
        if len(window) < 2:
            return False
        
        # Calculate overall window imbalance
        total_delta = sum(b.delta for b in window)
        total_volume = sum(b.volume for b in window)
        if total_volume <= 0:
            return False
        
        window_imbalance = total_delta / total_volume
        
        # Check last bar's imbalance
        last_bar = window[-1]
        last_volume = last_bar.volume
        if last_volume <= 0:
            return False
        
        last_imbalance = last_bar.delta / last_volume
        
        # If window is bullish but last bar is bearish (or vice versa),
        # flow is reversing
        if window_imbalance > threshold and last_imbalance < -threshold:
            return True
        if window_imbalance < -threshold and last_imbalance > threshold:
            return True
        
        return False

    def _is_flow_consistent(self, window, threshold: float) -> bool:
        """
        Check if flow is consistently directional across the window.
        If too many bars contradict the overall direction, the signal is weak.
        
        Returns True if flow is consistent enough to trade.
        """
        if len(window) < 2:
            return True
        
        # Calculate overall window imbalance
        total_delta = sum(b.delta for b in window)
        total_volume = sum(b.volume for b in window)
        if total_volume <= 0:
            return False
        
        window_imbalance = total_delta / total_volume
        
        # Count how many bars agree with the overall direction
        agreeing_bars = 0
        for bar in window:
            if bar.volume <= 0:
                continue
            bar_imbalance = bar.delta / bar.volume
            
            # For bullish signals, count bars with positive delta
            if window_imbalance > 0 and bar.delta > 0:
                agreeing_bars += 1
            # For bearish signals, count bars with negative delta
            elif window_imbalance < 0 and bar.delta < 0:
                agreeing_bars += 1
        
        # Require at least 60% of bars to agree with overall direction
        consistency_ratio = agreeing_bars / len(window)
        return consistency_ratio >= 0.6

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

        # Check for flow reversal and consistency
        if self._is_flow_reversing(fast_window, threshold):
            return
        
        if not self._is_flow_consistent(fast_window, threshold):
            return

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

        # Entry logic:
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - Flow must not be reversing
        # - Flow must be consistent across the lookback window

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
