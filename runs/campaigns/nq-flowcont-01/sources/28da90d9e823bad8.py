"""
Order flow continuation with adaptive multi-bar confirmation and session-aware filtering.

Analysis of trial progression reveals a critical insight:
- Trial #1 (score -0.4823): Best performer with ~35k trades
- Trial #7 (score -0.8645): Recent attempt with strict multi-bar confirmation, ~21k trades
- Trial #2-6: All worse, ranging -0.94 to -0.96

Trial #7's approach (multi-bar confirmation + volume filter) reduced trades to 21k
but still underperformed #1. The problem: we're being too strict and filtering out
valid signals. Trial #1's adaptive volatility threshold was elegant and worked.

Key insight from the data:
1. Trial #1 succeeded with simple logic: adaptive threshold + slow timeframe agreement
2. Adding complexity (entropy, multi-bar confirmation, volume filters) made it worse
3. The best approach trades frequently on quality signals, not rarely on "perfect" ones
4. Commission is real but can be beaten with sufficient edge on each trade

New approach - return to first principles with refinements:
1. Use trial #1's proven adaptive volatility threshold (it worked)
2. Simplify the slow timeframe filter: just require same-direction agreement
3. Add session-aware context: scale threshold based on session time (morning vs afternoon)
4. Trade more frequently but maintain signal quality through adaptive threshold
5. Use the bracket to manage risk automatically
6. Size at 1 contract to minimize per-trade commission impact
7. Keep lookback windows moderate (5 bars fast, 4 bars slow) for responsiveness

The hypothesis: Trial #1's success came from the right balance of:
- Trading on frequent valid signals (not over-filtering)
- Using volatility-adaptive thresholds (not fixed thresholds)
- Simple multi-timeframe confirmation (not over-constraining)

Session-aware scaling adds a refinement: morning sessions (around open) have different
flow characteristics than afternoon, so we adapt our sensitivity accordingly. This
captures intraday regime differences without over-complicating the logic.
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

    def _get_session_time_factor(self, ctx) -> float:
        """
        Return a multiplier for threshold based on time of day within session.
        Morning (first 2 hours): 0.9x (more lenient, higher activity)
        Afternoon (last 2 hours): 1.1x (stricter, lower activity)
        Rest: 1.0x (normal)
        """
        if ctx.session_end <= ctx.session_start:
            return 1.0
        
        session_duration = ctx.session_end - ctx.session_start
        time_in_session = ctx.ts - ctx.session_start
        
        # Morning: first 2 hours
        if time_in_session < 2 * 3600 * 1_000_000_000:
            return 0.9
        
        # Afternoon: last 2 hours
        if time_in_session > session_duration - 2 * 3600 * 1_000_000_000:
            return 1.1
        
        return 1.0

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

        # Apply session-time factor to threshold
        session_factor = self._get_session_time_factor(ctx)
        threshold = threshold * session_factor

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
        # - Fast imbalance must exceed adaptive threshold (adjusted for time of day)
        # - Slow imbalance must be in same direction (confirming trend)

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
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
