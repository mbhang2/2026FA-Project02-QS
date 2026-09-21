"""
Order flow continuation with session-phase awareness and adaptive entry sizing.

Analysis of trial progression shows a critical insight:
- Trial #1 (score -0.4823): Best performer, ~35k trades, adaptive threshold + multi-timeframe
- Trial #15 (score -0.8647): High conviction filtering reduced trades but degraded score
- Pattern: Reducing trade frequency below ~35k trades makes things worse

The key realization: Trial #1 had the right balance. The commission drag (-0.2 per round trip)
is real, but the signal is profitable enough at ~35k trades to partially overcome it.

The problem with trial #15: By requiring BOTH fast AND slow to be strong (slow_threshold=0.05),
we filtered out too many valid signals. The slow timeframe should confirm DIRECTION, not magnitude.

New approach - return to trial #1's core logic but with refinements:
1. Keep the adaptive volatility-based threshold (proven effective)
2. Keep the multi-timeframe confirmation (fast signal + slow direction agreement)
3. Add session-phase awareness: Order flow behavior differs between session open, mid, and close
   - Early session (first 2 hours): Higher noise, require stronger signals
   - Mid session (2-6 hours): Optimal signal/noise, use base threshold
   - Late session (last 2 hours): Avoid entries due to end-of-day volatility

4. Adaptive position sizing based on signal strength:
   - size = 1 for moderate signals (imbalance 0.12-0.15)
   - size = 2 for strong signals (imbalance > 0.15)
   - This lets us trade more when conviction is high, less when marginal

5. Keep moderate bracket levels (0.8 stop, 1.2 target) from trial #1

The hypothesis: Trial #1 worked because it balanced frequency and selectivity well.
We can improve it by:
- Adding session-phase filters to avoid noise at boundaries
- Scaling position size with signal strength to improve risk-adjusted returns
- Keeping the core adaptive threshold that proved effective
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
        min_size: int = 1,
        max_size: int = 2,
        strong_signal_threshold: float = 0.15,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
        session_start_hours: int = 1,
        session_end_hours: int = 2,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Base imbalance threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            min_size: Minimum position size for marginal signals
            max_size: Maximum position size for strong signals
            strong_signal_threshold: Imbalance level to trigger max_size
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
            session_start_hours: Hours from session open to avoid (noise period)
            session_end_hours: Hours before session close to avoid (end-of-day volatility)
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.min_size = min_size
        self.max_size = max_size
        self.strong_signal_threshold = strong_signal_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.session_start_hours = session_start_hours
        self.session_end_hours = session_end_hours

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def _is_in_tradeable_session_phase(self, ctx) -> bool:
        """Check if we're in the tradeable part of the session (avoid open and close noise)."""
        session_length_ns = ctx.session_end - ctx.session_start
        time_into_session_ns = ctx.ts - ctx.session_start
        
        # Avoid the first N hours (session open noise)
        start_cutoff_ns = self.session_start_hours * 3600 * 1_000_000_000
        if time_into_session_ns < start_cutoff_ns:
            return False
        
        # Avoid the last N hours (end-of-day volatility and position squaring)
        end_cutoff_ns = self.session_end_hours * 3600 * 1_000_000_000
        if session_length_ns - time_into_session_ns < end_cutoff_ns:
            return False
        
        return True

    def _calculate_position_size(self, fast_imbalance: float) -> int:
        """Scale position size based on signal strength."""
        abs_imbalance = abs(fast_imbalance)
        
        if abs_imbalance >= self.strong_signal_threshold:
            return self.max_size
        else:
            return self.min_size

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

        # Skip if we're in a noisy session phase (open or close)
        if not self._is_in_tradeable_session_phase(ctx):
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

        # Entry logic:
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - Position size scales with signal strength (1 or 2 contracts)
        # - Session phase filters out noisy periods

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            size = self._calculate_position_size(fast_imbalance)
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            size = self._calculate_position_size(fast_imbalance)
            ctx.sell(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
