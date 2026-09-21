"""
Order flow continuation with regime-aware entry filtering and position management.

Analysis of trial history shows:
- Trial #1 (best): score -0.4823, ~35k trades - simple adaptive threshold + multi-timeframe
- Trial #19: score -0.9599, ~106k trades - session phasing reduced entries but increased losses
- Trial #18: score -0.8289, ~30k trades - trade frequency limiting helped slightly
- Most refinements: worse than -0.75

Key observation: Trial #1's simplicity was the strength. It traded frequently with a real signal.
Later attempts to improve via session phasing, trade frequency limiting, or more complex filters
all degraded performance, suggesting we're fighting against the signal rather than enhancing it.

The core issue: The order flow continuation signal is real but weak. Commission is 0.20 per round turn.
Trial #1 made ~35k trades, so total commission drag was ~$7k on a dataset with limited profit.

New hypothesis - focus on SIGNAL QUALITY not FREQUENCY:
1. Keep trial #1's adaptive volatility threshold (it worked)
2. Keep multi-timeframe confirmation (it worked)
3. Add a FILTER: Only trade when recent bars show NO strong reversal
   - If the last N bars show strong opposite flow, skip the entry
   - This catches false signals where flow reverses immediately
4. Add a FILTER: Only trade when the bar that generated the signal has real volume
   - Filter out thin bars where the signal might be noise
5. Tighten the slow_imbalance requirement slightly (require it to exceed a small threshold)
   - This rejects weak confirmations
6. Use size=1 (more robust than size=2)
7. Keep moderate brackets from trial #1 (stop_offset=0.8, target_offset=1.2)

The math: Trial #1 made 35k trades at score -0.4823. If we can reduce false signals by 20-30%
(filtering reversals and thin bars), we might improve to -0.35 to -0.40 range while keeping
the true signal trades.

This approach respects the data: trial #1 found something real. We're not over-engineering
or adding regime complexity; we're just filtering out the obvious false positives that
the simple signal misses.
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
        reversal_lookback: int = 2,
        min_volume_percentile: float = 0.4,
        slow_threshold: float = 0.01,
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
            reversal_lookback: Bars to check for recent reversal signals
            min_volume_percentile: Minimum volume as fraction of recent average
            slow_threshold: Minimum slow imbalance magnitude
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
        self.reversal_lookback = reversal_lookback
        self.min_volume_percentile = min_volume_percentile
        self.slow_threshold = slow_threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def _has_recent_reversal(self, fast_history, lookback_period: int) -> bool:
        """
        Check if recent bars show strong opposite flow.
        If the signal is for a long but the last few bars show strong selling,
        it's likely a false signal (reversal pattern).
        """
        if len(fast_history) < lookback_period + 1:
            return False
        
        recent = fast_history.last(lookback_period)
        reversal_delta = sum(b.delta for b in recent)
        reversal_volume = sum(b.volume for b in recent)
        
        if reversal_volume <= 0:
            return False
        
        # If recent bars show strong opposite flow (imbalance > 0.15 in either direction),
        # it suggests reversal is happening
        reversal_imbalance = abs(reversal_delta / reversal_volume)
        return reversal_imbalance > 0.15

    def _is_volume_sufficient(self, fast_history, current_bar_volume: int) -> bool:
        """
        Check if the current bar has sufficient volume compared to recent average.
        Filters out thin bars where the signal might be noise.
        """
        if len(fast_history) < self.volatility_window:
            return True  # Not enough history, don't filter
        
        recent_bars = fast_history.last(self.volatility_window)
        avg_volume = sum(b.volume for b in recent_bars) / len(recent_bars)
        
        # Current bar volume must be at least min_volume_percentile of average
        return current_bar_volume >= (avg_volume * self.min_volume_percentile)

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

        # Get current bar for volume and reversal checks
        current_bar = fast_history[-1]
        
        # FILTER 1: Check for recent reversal (strong opposite flow in last few bars)
        if self._has_recent_reversal(fast_history, self.reversal_lookback):
            return

        # FILTER 2: Check that current bar has sufficient volume
        if not self._is_volume_sufficient(fast_history, current_bar.volume):
            return

        # Entry logic (trial #1 base with filters):
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction AND exceed minimum threshold
        # - Recent bars must not show reversal
        # - Current bar must have sufficient volume

        if fast_imbalance > threshold and slow_imbalance > self.slow_threshold:
            # Strong buying pressure on fast, confirmed by slow, no reversal signal
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < -self.slow_threshold:
            # Strong selling pressure on fast, confirmed by slow, no reversal signal
            ctx.sell(
                size=self.size,
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
