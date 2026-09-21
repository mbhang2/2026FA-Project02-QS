"""
Order flow continuation with refined signal detection and session structure awareness.

Analysis of trial progression reveals critical patterns:
- Trial #1 (score -0.4823): Best performer, ~35k trades, adaptive volatility threshold
- Trial #9 (crashed): Signal strength scaling with int() conversion failed
- Trials #2-8: All worse than #1, ranging -0.85 to -0.97

The core issue with trial #9: Converting float size to int() with min_size=0.5 
results in size=0 for weak signals, which violates the "positive whole number" 
constraint. This is a fundamental design flaw.

Key insights from successful trial #1:
1. Adaptive volatility threshold works (0.8 + 0.4 * volatility_ratio multiplier)
2. Multi-timeframe confirmation (fast signal + slow trend agreement) is valuable
3. Moderate trading frequency (~35k trades) beats both over-trading and under-trading
4. Simple logic outperforms complex signal strength scaling

The hypothesis: Trial #1 succeeded because it balanced signal quality with frequency.
The problem: We've been trying to add complexity (session-aware, strength scaling, 
entropy) when the simple approach was already near-optimal.

New approach - return to trial #1 fundamentals with targeted refinements:
1. Keep the proven adaptive volatility threshold from trial #1
2. Keep the simple multi-timeframe confirmation (fast + slow agreement)
3. Add one refinement: session structure awareness
   - Identify when we're near session boundaries (last 30 minutes)
   - Reduce position size or skip entries near close to avoid overnight risk
   - This addresses a real risk (overnight gaps) without over-complicating logic
4. Fixed size of 1 contract (no fractional sizing that causes int() issues)
5. Conservative bracket offsets that have proven effective
6. Track session boundaries explicitly to make decisions clear

This is intentionally conservative: we're not trying to beat trial #1, we're trying
to understand why it worked and make it more robust. The session boundary awareness
is a practical addition that reduces overnight risk without requiring complex 
signal strength calculations.
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
        close_buffer_minutes: int = 30,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Base imbalance threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            size: Position size (fixed)
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
            close_buffer_minutes: Minutes before session close to stop entering
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
        self.close_buffer_ns = close_buffer_minutes * 60 * 1_000_000_000

    def _is_near_session_close(self, ctx) -> bool:
        """
        Check if we're within the close buffer period.
        Returns True if we should avoid new entries to prevent overnight risk.
        """
        time_to_close = ctx.session_end - ctx.ts
        return time_to_close < self.close_buffer_ns

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

        # Skip entries near session close to avoid overnight risk
        if self._is_near_session_close(ctx):
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

        # Entry logic:
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - This is the proven approach from trial #1

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

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
