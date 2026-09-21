"""
Order flow continuation with reduced trade frequency and tighter commission management.

Analysis of prior trials reveals a critical pattern:
- Trial #1 (score -0.4823): Best performer, ~35k trades
- Trial #2 (score -0.9683): ~138k trades, massive overtrading
- Trial #3-6: All worse than #1, ranging from -0.94 to -0.96

The clear insight: Trial #1's success came from trading *less frequently* while
maintaining signal quality. More recent trials added complexity (entropy, momentum,
stricter filters) but all performed worse. The problem is not missing filters—it's
that we're trading too much on marginal signals.

Key observations:
1. Trial #1 had ~35k trades with -0.4823 score
2. Trial #2 had ~138k trades with -0.9683 score (3.9x more trades, much worse)
3. Commission is 0.1 index points per side (0.2 round trip)
4. At NQ's $20/point, this is $4 per contract per round trip
5. High frequency trading at this commission is mathematically losing

New approach - focus on signal strength and trade selectivity:
1. Keep the adaptive volatility threshold from #1 (it was proven best)
2. Significantly raise the threshold to trade only on the strongest signals
3. Require multi-bar confirmation: not just current bar, but the previous bar too
4. Add a minimum volume filter: only trade when volume is above recent average
5. Increase bracket offsets to better handle commission
6. Keep multi-timeframe confirmation simple (same direction agreement)
7. Reduce size to 1 to minimize commission impact per trade

The hypothesis: Order flow continuation is real, but only for strong, consistent,
high-volume signals. Weak signals that barely cross the threshold are net losers
after commission. By trading only the highest-conviction setups, we reduce
frequency dramatically (aiming for ~10-15k trades) while maintaining or improving
the score.

This is a direct response to the trial #1 success pattern: fewer, higher-quality
trades beat many marginal ones when commission is material.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.20,  # Raised from 0.12 to trade only strongest signals
        volatility_window: int = 20,
        volume_lookback: int = 10,  # Check volume vs recent average
        size: int = 1,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Base imbalance threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            volume_lookback: Bars to measure average volume over
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
        self.volume_lookback = volume_lookback
        self.size = size
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

        # Need enough history for all calculations
        if len(fast_history) < self.lookback_fast + 1:  # +1 for previous bar confirmation
            return
        if len(slow_history) < self.lookback_slow:
            return
        if len(fast_history) < self.volume_lookback:
            return

        # Get the current and previous bars for confirmation
        current_bar = fast_history[-1]
        prev_bar = fast_history[-2] if len(fast_history) >= 2 else None
        
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

        # Check volume: only trade when volume is above recent average
        # This filters out low-conviction setups that happen during thin markets
        volume_window = fast_history.last(self.volume_lookback)
        avg_volume = sum(b.volume for b in volume_window) / len(volume_window) if volume_window else 0
        
        if current_bar.volume < avg_volume * 0.8:
            # Volume is below average, skip this signal
            return

        # Check previous bar confirmation: previous bar's delta must be in same direction
        # This ensures the flow is not just a single-bar spike but a continuation
        if prev_bar is None:
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

        # Entry logic with multi-bar confirmation:
        # - Fast imbalance must exceed (high) adaptive threshold
        # - Previous bar must show same direction (confirmation)
        # - Current bar must show same direction (continuation)
        # - Slow imbalance must be in same direction (trend agreement)
        # - Volume must be above average (conviction)

        if (fast_imbalance > threshold and 
            prev_bar.delta > 0 and 
            current_bar.delta > 0 and 
            slow_imbalance > 0):
            # Strong, confirmed buying pressure with above-average volume
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif (fast_imbalance < -threshold and 
              prev_bar.delta < 0 and 
              current_bar.delta < 0 and 
              slow_imbalance < 0):
            # Strong, confirmed selling pressure with above-average volume
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
