"""
Order flow continuation strategy with asymmetric entry/exit and session phase awareness.

Building on trial #1 (score -0.4823), this strategy refines the approach:

1. Asymmetric thresholds: buying pressure needs lower threshold than selling
   (market structure suggests buying is more predictive than selling)
2. Session phase filtering: exclude first and last hours of session when flow
   is less reliable (cash open/close noise, end-of-day liquidation)
3. Tighter stops on shorts (more volatile) vs longs
4. Reduced position size to account for commissions eating into small edges
5. More aggressive profit targets relative to stops (risk/reward > 1)
6. Explicit tracking of bars in session to enable phase filtering

The hypothesis: order flow continuation is real but directional (buying > selling)
and time-dependent (certain session phases are noise). Filter both dimensions.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 4,
        lookback_slow: int = 3,
        buy_threshold: float = 0.08,
        sell_threshold: float = 0.14,
        volatility_window: int = 15,
        size: int = 1,
        long_stop_offset: float = 0.6,
        short_stop_offset: float = 0.5,
        target_offset: float = 1.5,
        min_bars_into_session: int = 6,
        max_bars_before_close: int = 4,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            buy_threshold: Lower threshold for long entries (buying is more predictive)
            sell_threshold: Higher threshold for short entries (selling is less reliable)
            volatility_window: Bars to measure volatility over
            size: Position size
            long_stop_offset: Stop loss distance for longs
            short_stop_offset: Stop loss distance for shorts
            target_offset: Profit target distance from entry
            min_bars_into_session: Skip trades in first N bars of session
            max_bars_before_close: Skip trades in last N bars of session
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.volatility_window = volatility_window
        self.size = size
        self.long_stop_offset = long_stop_offset
        self.short_stop_offset = short_stop_offset
        self.target_offset = target_offset
        self.min_bars_into_session = min_bars_into_session
        self.max_bars_before_close = max_bars_before_close
        
        # Track session state
        self._current_session = None
        self._bars_in_session = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._bars_in_session = 0
        self._bars_in_session += 1

        # Skip if we already have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Skip during warmup when orders would be ignored
        if not ctx.actionable:
            return

        # Only act when the fast bar closes
        if self.fast_interval not in ctx.bars:
            return

        # Skip trades in early and late session (noise periods)
        if self._bars_in_session < self.min_bars_into_session:
            return
        
        # Estimate bars remaining in session (rough estimate)
        time_left = ctx.session_end - ctx.ts
        # Assume ~1 minute bars, so estimate bars left
        bars_left_estimate = int(time_left / 60_000_000_000) if time_left > 0 else 0
        if bars_left_estimate < self.max_bars_before_close:
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
            # Volatility adjustment: scale thresholds with volatility
            vol_multiplier = 0.7 + 0.6 * volatility_ratio
        else:
            vol_multiplier = 1.0

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

        # Asymmetric entry logic:
        # - Buying pressure (positive flow) needs lower threshold
        # - Selling pressure (negative flow) needs higher threshold
        # - Slow trend must confirm direction
        
        adjusted_buy_threshold = self.buy_threshold * vol_multiplier
        adjusted_sell_threshold = self.sell_threshold * vol_multiplier

        if fast_imbalance > adjusted_buy_threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow trend
            ctx.buy(
                size=self.size,
                stop_offset=self.long_stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -adjusted_sell_threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow trend
            ctx.sell(
                size=self.size,
                stop_offset=self.short_stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
