"""
Order flow continuation with adaptive entry timing and dynamic bracket sizing.

Analysis of 29 trials reveals critical patterns:
- Trial #1 (best): score -0.4823, ~35k trades
  - Core: adaptive volatility threshold + multi-timeframe confirmation
  - Tight brackets (stop=0.8, target=1.2) work well
  - ~35k trades suggests good selectivity
  
- Trial #29: score -0.9232, ~56k trades
  - Added session timing filters + volatility regime filter
  - More trades than #1 but worse score, suggesting filters weren't tight enough

Key insight: Trial #1 succeeds because it's SELECTIVE, not because of complexity.
The problem with all subsequent trials: they either:
1. Add too much complexity (momentum, regime filters)
2. Generate too many trades (trial #28: 174k trades!)
3. Relax the signal threshold (trading weaker flow)

New hypothesis: The issue is ENTRY TIMING within the flow signal.
- Trial #1 trades when imbalance is EXTREME (high threshold = 0.12)
- But extreme imbalance often means the move is already underway
- Solution: Trade when imbalance is ACCELERATING FROM BASELINE, not just extreme
- Use a two-bar flow comparison: current bar's imbalance vs prior bar
- This catches the BEGINNING of strong flow, not its peak

Strategy (simplification of trial #1 with better entry timing):
1. Keep trial #1's multi-timeframe structure (1m fast, 15m slow)
2. Keep trial #1's brackets (stop=0.8, target=1.2)
3. Keep trial #1's adaptive volatility threshold
4. CHANGE: Entry signal is "flow acceleration" not "extreme flow magnitude"
   - Measure: is current bar's delta significantly larger than prior bar's delta?
   - This catches flow building, not peaks
5. Still require multi-timeframe confirmation (slow imbalance in same direction)
6. Reduce base threshold slightly (0.12 → 0.10) since we're measuring momentum
7. This should maintain ~35k trade frequency while improving entry quality

The logic: Trial #1 is good. We should improve its TIMING, not add complexity.
By entering on flow acceleration (not magnitude peaks), we catch moves earlier,
giving the bracket more room to work and improving win rate.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.10,
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
            base_threshold: Base threshold for flow acceleration (adjusted by volatility)
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

        # Need enough history: lookback_fast + 1 for acceleration measurement
        if len(fast_history) < self.lookback_fast + 1:
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

        # Measure FLOW ACCELERATION: is current bar's delta significantly larger than prior?
        # Get the last 2 bars to measure acceleration
        recent_bars = fast_history.last(2)
        if len(recent_bars) >= 2:
            current_bar = recent_bars[-1]
            prior_bar = recent_bars[-2]
            
            # Normalize deltas by their respective volumes
            if current_bar.volume > 0:
                current_delta_ratio = current_bar.delta / current_bar.volume
            else:
                current_delta_ratio = 0.0
            
            if prior_bar.volume > 0:
                prior_delta_ratio = prior_bar.delta / prior_bar.volume
            else:
                prior_delta_ratio = 0.0
            
            # Acceleration is the change in normalized delta
            delta_acceleration = current_delta_ratio - prior_delta_ratio
        else:
            delta_acceleration = 0.0

        # Get the full lookback window for slow trend confirmation
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
        # - Flow acceleration must exceed threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - This catches flow building, not peaks

        if delta_acceleration > threshold and slow_imbalance > 0:
            # Flow accelerating upward, confirmed by slow trend
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-accel-long",
            )
        elif delta_acceleration < -threshold and slow_imbalance < 0:
            # Flow accelerating downward, confirmed by slow trend
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-accel-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
