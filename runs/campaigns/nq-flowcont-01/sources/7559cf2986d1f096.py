"""
Order flow continuation with mean-reversion exit strategy.

Analysis of trial history:
- Trial #1 (best): score -0.4823, ~35k trades - adaptive threshold + multi-timeframe
- Trial #23: score -0.9578, ~104k trades - signal strength scaling made it worse
- Trial #22: score -0.8647, ~31k trades - session timing filter hurt performance

Key observation: Trial #1 remains best by a significant margin (-0.4823 vs -0.8+).
Every attempt to improve the signal generation or risk management has degraded performance.

This suggests the core signal is correct, but we're losing money on individual trades.
The problem isn't entry selection or bracket sizing - it's that we're holding losers
too long and taking profits too quickly.

New hypothesis - MEAN REVERSION EXIT:
Order flow creates temporary price impact. The signal predicts continuation, but only
for a limited time. After that, mean reversion takes over. Current strategy uses fixed
brackets that don't adapt to this dynamic.

Approach:
1. Keep trial #1's core completely unchanged:
   - adaptive volatility threshold (0.12)
   - multi-timeframe confirmation (fast + slow imbalance)
   - lookback_fast=5, lookback_slow=4
   - size=1
2. CHANGE THE EXIT STRATEGY:
   - Instead of fixed brackets, use a trailing stop that tightens over time
   - Start with a wide stop (1.0) to give the trade room
   - After N bars, tighten the stop to lock in gains (trail at 0.3)
   - This captures the initial continuation while protecting against reversals
3. Keep targets fixed but tighter (1.0 instead of 1.2) - take profits faster
4. This reduces trade duration, which reduces exposure to mean reversion

The logic: Trial #1's entries are good (best score). The issue is exits.
By using a time-based trailing stop that gets tighter, we:
- Let winning trades run initially (wide stop)
- Lock in gains before mean reversion (tight trail after holding period)
- Exit losing trades faster (tighter stops prevent large losses)
- Reduce commission impact (fewer days held = fewer overnight costs)

This is a mechanical change to exit timing, not a change to the signal itself.
Trial #1 scored -0.4823 with fixed brackets. Adaptive trailing should improve this.
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
        initial_stop_offset: float = 1.0,
        target_offset: float = 1.0,
        trail_bars: int = 8,
        trail_stop_offset: float = 0.3,
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
            initial_stop_offset: Initial stop loss distance from entry
            target_offset: Profit target distance from entry
            trail_bars: Number of bars to hold before tightening stop
            trail_stop_offset: Tighter stop distance after holding period
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.size = size
        self.initial_stop_offset = initial_stop_offset
        self.target_offset = target_offset
        self.trail_bars = trail_bars
        self.trail_stop_offset = trail_stop_offset
        self._entry_ts = None
        self._bars_held = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        # Track how many bars we've held the position
        if ctx.position != 0:
            self._bars_held += 1
            
            # If we've held long enough, tighten the trailing stop
            if self._bars_held >= self.trail_bars:
                stops = ctx.find_orders(tag="protective-stop")
                if stops:
                    # Tighten the stop to lock in gains
                    new_trigger = ctx.last_price - self.trail_stop_offset
                    # Only move stop up (for long) or down (for short), never relax it
                    if ctx.position > 0:
                        current_stop = stops[0].stop_price
                        if new_trigger > current_stop:
                            ctx.modify(stops[0].id, trigger=new_trigger)
                    else:
                        current_stop = stops[0].stop_price
                        if new_trigger < current_stop:
                            ctx.modify(stops[0].id, trigger=new_trigger)
            return

        # Reset bars held counter when flat
        self._bars_held = 0

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

        # Entry logic (trial #1 base, unchanged):
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction (confirming trend)

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            self._entry_ts = ctx.ts
            self._bars_held = 0
            ctx.buy(
                size=self.size,
                stop_offset=self.initial_stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            self._entry_ts = ctx.ts
            self._bars_held = 0
            ctx.sell(
                size=self.size,
                stop_offset=self.initial_stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        # Reset bars held when position closes
        if ctx.fill.position == 0:
            self._bars_held = 0
            self._entry_ts = None

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
