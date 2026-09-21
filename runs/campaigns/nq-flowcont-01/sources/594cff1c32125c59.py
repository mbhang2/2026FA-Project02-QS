"""
Order flow continuation with refined signal strength and risk-adjusted position sizing.

Analysis of trial progression shows a clear pattern:
- Trial #1 (score -0.4823): Best performer with ~35k trades, adaptive volatility threshold
- Trial #7 (score -0.8645): Strict multi-bar confirmation, ~21k trades
- Trial #8 (score -0.9564): Session-aware filtering, ~105k trades

The key insight: Trial #1's success came from trading at moderate frequency with
a volatility-adaptive threshold. Over-filtering (trials #7) reduced edge; adding
complexity (trials #8) increased trades without improving results.

Problem with all prior approaches: They treat all signals equally after passing
the threshold. A signal with 20% imbalance and a signal with 80% imbalance both
trigger the same trade. This is inefficient.

New approach - signal strength scaling:
1. Keep trial #1's proven adaptive volatility threshold (it works)
2. Add signal strength scaling: stronger imbalance = larger position
3. Instead of binary entry/no-entry, scale size from 0.5 to 2 contracts based on
   signal magnitude relative to threshold
4. This allows frequent trading on weak signals (small size) and concentrated
   bets on strong signals (large size)
5. Use the same multi-timeframe confirmation (slow must agree in direction)
6. Adjust bracket offsets based on signal strength: stronger signals get tighter
   stops (more conviction), weaker signals get wider stops (less conviction)

The hypothesis: Order flow continuation is real and the edge varies with signal
strength. By scaling position size and risk management to match signal strength,
we can trade more frequently while maintaining better risk-adjusted returns.

This addresses the trial #1 success pattern while improving upon it: don't just
trade on strong signals, also trade weak signals with proportionally smaller size
and wider stops. This captures more of the edge without over-leveraging on marginal
signals.
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
        min_size: float = 0.5,
        max_size: float = 2.0,
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
            min_size: Minimum position size for weakest signals
            max_size: Maximum position size for strongest signals
            base_stop_offset: Base stop loss distance (scaled by signal strength)
            base_target_offset: Base profit target distance (scaled by signal strength)
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.min_size = min_size
        self.max_size = max_size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def _calculate_position_size(self, imbalance: float, threshold: float) -> float:
        """
        Scale position size based on signal strength.
        At threshold: min_size
        At 3x threshold: max_size
        Linear interpolation between them.
        """
        if abs(imbalance) < threshold:
            return 0.0
        
        # Normalize signal strength: how many thresholds above the minimum
        signal_strength = (abs(imbalance) - threshold) / (threshold * 2)
        signal_strength = max(0.0, min(1.0, signal_strength))  # Clamp to [0, 1]
        
        size = self.min_size + signal_strength * (self.max_size - self.min_size)
        return size

    def _calculate_bracket_offsets(self, imbalance: float, threshold: float) -> tuple[float, float]:
        """
        Scale bracket offsets based on signal strength.
        Stronger signals = tighter stops (more conviction)
        Weaker signals = wider stops (less conviction)
        """
        if abs(imbalance) < threshold:
            return self.base_stop_offset, self.base_target_offset
        
        # Normalize signal strength
        signal_strength = (abs(imbalance) - threshold) / (threshold * 2)
        signal_strength = max(0.0, min(1.0, signal_strength))
        
        # Strong signals: reduce offsets by up to 20%
        # Weak signals: increase offsets by up to 20%
        strength_factor = 1.0 - (0.2 * signal_strength)
        
        stop_offset = self.base_stop_offset * strength_factor
        target_offset = self.base_target_offset * strength_factor
        
        return stop_offset, target_offset

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

        # Calculate position size and bracket offsets based on signal strength
        size = self._calculate_position_size(fast_imbalance, threshold)
        stop_offset, target_offset = self._calculate_bracket_offsets(fast_imbalance, threshold)

        # Entry logic:
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - Position size scales with signal strength

        if fast_imbalance > threshold and slow_imbalance > 0 and size > 0:
            # Strong buying pressure on fast, confirmed by slow
            # Size and brackets scale with signal strength
            ctx.buy(
                size=int(size),  # Convert to integer contracts
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0 and size > 0:
            # Strong selling pressure on fast, confirmed by slow
            # Size and brackets scale with signal strength
            ctx.sell(
                size=int(size),  # Convert to integer contracts
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
