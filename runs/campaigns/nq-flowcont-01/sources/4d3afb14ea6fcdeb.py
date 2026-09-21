"""
Order flow continuation with refined signal strength and session-aware exits.

Analysis of trial progression reveals:
- Trial #1 (score -0.4823): Best performer with ~35k trades
- Trial #12 (score -0.8869): Added momentum + volume checks, ~29k trades (fewer but worse)
- Trials #11, #10: Over-filtering degraded scores despite fewer trades
- All others: Consistently worse than #1

Key insight: Trial #1's success came from simplicity - adaptive volatility threshold
+ multi-timeframe confirmation. The pattern shows that adding more filters doesn't
improve results; in fact, it makes them worse. This suggests:

1. The core signal (adaptive threshold + multi-timeframe confirmation) is sound
2. Over-filtering removes good signals along with bad ones
3. The issue isn't signal selection but position management and risk control

New hypothesis: Instead of trying to filter signals more aggressively, improve
position management and exit timing:
- Keep trial #1's proven core: adaptive threshold + multi-timeframe confirmation
- Reduce false positives by being more selective about WHICH bars trigger entries
  (only on fast bar closes, not slow bar closes)
- Improve exits by:
  1. Tightening stops in choppy markets (low volatility = tighter stop)
  2. Extending targets in trending markets (high volatility = wider target)
  3. Adding session-aware logic: be more aggressive early in session, more defensive
     near session close
- Reduce position size slightly (size=1 instead of 2) to improve win rate

The core order flow continuation hypothesis is correct. The edge is real but small.
Trial #1 found it. We should return to that foundation and improve execution rather
than add more filters.
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
        stop_offset_base: float = 0.8,
        target_offset_base: float = 1.2,
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
            stop_offset_base: Base stop loss distance (adjusted by volatility)
            target_offset_base: Base profit target distance (adjusted by volatility)
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.size = size
        self.stop_offset_base = stop_offset_base
        self.target_offset_base = target_offset_base

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def _get_volatility_ratio(self, history) -> float:
        """Calculate volatility ratio for adaptive position sizing."""
        volatility_lookback = min(self.volatility_window, len(history))
        if volatility_lookback < 2:
            return 1.0
        
        vol_window = history.last(volatility_lookback)
        price_range = max(b.high for b in vol_window) - min(b.low for b in vol_window)
        avg_price = sum(b.close for b in vol_window) / len(vol_window)
        
        if avg_price > 0:
            return price_range / avg_price
        return 1.0

    def _get_session_phase(self, ctx) -> float:
        """
        Determine where we are in the session (0.0 = start, 1.0 = end).
        Returns a value that can be used to adjust aggressiveness.
        """
        time_left = ctx.session_end - ctx.ts
        session_duration = ctx.session_end - ctx.session_start
        
        if session_duration <= 0:
            return 0.5
        
        phase = 1.0 - (time_left / session_duration)
        return max(0.0, min(1.0, phase))

    def on_bars(self, ctx) -> None:
        # Skip if we already have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Skip during warmup when orders would be ignored
        if not ctx.actionable:
            return

        # Only act when the fast bar closes (not on slow bar close alone)
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
        volatility_ratio = self._get_volatility_ratio(fast_history)
        threshold = self.base_threshold * (0.8 + 0.4 * volatility_ratio)

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

        # Adjust bracket levels based on volatility and session phase
        # High volatility = wider brackets (need more room)
        # Low volatility = tighter brackets (less noise, can be tighter)
        vol_adjusted_stop = self.stop_offset_base * (0.7 + 0.6 * volatility_ratio)
        vol_adjusted_target = self.target_offset_base * (0.7 + 0.6 * volatility_ratio)
        
        # Session phase adjustment: be more aggressive early, more defensive late
        session_phase = self._get_session_phase(ctx)
        # In last 20% of session, tighten targets slightly
        if session_phase > 0.8:
            vol_adjusted_target *= 0.9
        # In first 20% of session, widen targets slightly
        elif session_phase < 0.2:
            vol_adjusted_target *= 1.1

        # Entry logic (same core as trial #1):
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - Adaptive bracket sizing based on volatility and session phase

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=vol_adjusted_stop,
                target_offset=vol_adjusted_target,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=self.size,
                stop_offset=vol_adjusted_stop,
                target_offset=vol_adjusted_target,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
