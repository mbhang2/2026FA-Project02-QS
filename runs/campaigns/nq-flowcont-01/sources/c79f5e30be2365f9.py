"""
Order flow continuation with flow momentum and regime-aware entry timing.

Analysis of 27 prior trials shows:
- Trial #1 (best): score -0.4823, ~35k trades
  - Core: adaptive volatility threshold + multi-timeframe confirmation
  - Brackets: stop_offset=0.8, target_offset=1.2
- Trial #27: score -0.9554, ~102k trades
  - Returned to trial #1 signal with wider brackets (stop=1.0, target=1.5)
  - Wider brackets made things worse, not better

Key insight: The problem is NOT the brackets or signal selectivity.
Trial #1 works because of its ADAPTIVE THRESHOLD and MULTI-TIMEFRAME CONFIRMATION.

New hypothesis: We're trading flow signals that are already fading.
- Current approach: trade when imbalance is EXTREME (threshold)
- Problem: Extreme imbalance often means the move is already over
- Solution: Trade when imbalance is BUILDING (momentum), not when it's peaked

New strategy - FLOW MOMENTUM instead of FLOW MAGNITUDE:
1. Keep trial #1's multi-timeframe structure (1m fast, 15m slow)
2. Replace "is imbalance extreme?" with "is imbalance ACCELERATING?"
   - Measure delta momentum: is current bar's delta stronger than prior bar's?
   - This catches flow BUILDING, not flow that has already peaked
3. Still require multi-timeframe confirmation (slow imbalance in same direction)
4. Use trial #1's brackets (stop=0.8, target=1.2) which worked
5. Add a volume confirmation: only trade when volume is above average
   - This filters out low-conviction, thin-volume signals
   - Ensures we're trading real order flow, not noise

Expected outcome:
- Same trade frequency as trial #1 (~35k trades)
- Better win rate because we're catching flow momentum, not peaks
- Score improvement: -0.4823 → -0.35 to -0.40 range

The logic: Trial #1's adaptive threshold is good, but it measures MAGNITUDE.
By measuring MOMENTUM (acceleration of flow), we enter earlier in the move
and avoid the problem of trading already-peaked imbalances.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.08,
        volatility_window: int = 20,
        momentum_lookback: int = 2,
        volume_percentile: float = 0.6,
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
            base_threshold: Base momentum threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            momentum_lookback: Bars to measure flow momentum over
            volume_percentile: Minimum volume as percentile of recent average
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
        self.momentum_lookback = momentum_lookback
        self.volume_percentile = volume_percentile
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

        # Need enough history for both calculations
        if len(fast_history) < max(self.lookback_fast, self.momentum_lookback + 1):
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

        # Calculate FLOW MOMENTUM: is current delta stronger than prior?
        # This catches flow BUILDING rather than flow that has peaked
        momentum_window = fast_history.last(self.momentum_lookback + 1)
        if len(momentum_window) >= 2:
            current_bar = momentum_window[-1]
            prior_bar = momentum_window[-2]
            
            current_delta = current_bar.delta
            prior_delta = prior_bar.delta
            
            # Momentum is the change in delta
            delta_momentum = current_delta - prior_delta
            
            # Normalize by current bar's volume to avoid bias from thin bars
            if current_bar.volume > 0:
                normalized_momentum = delta_momentum / current_bar.volume
            else:
                normalized_momentum = 0.0
        else:
            normalized_momentum = 0.0

        # Get the full lookback window for flow measurement
        fast_window = fast_history.last(self.lookback_fast)

        # Calculate average volume to filter thin signals
        avg_volume = sum(b.volume for b in fast_window) / len(fast_window) if fast_window else 1
        current_volume = fast_history[-1].volume if fast_history else 0

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

        # ENTRY LOGIC:
        # 1. Flow momentum must be building in the direction we want to trade
        # 2. Slow imbalance must confirm the direction
        # 3. Current volume must be above the percentile threshold
        # 4. This catches flow acceleration, not peaks

        volume_threshold = avg_volume * self.volume_percentile

        if (normalized_momentum > threshold and 
            slow_imbalance > 0 and 
            current_volume >= volume_threshold):
            # Flow momentum building to the upside, confirmed by slow trend
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-momentum-long",
            )
        elif (normalized_momentum < -threshold and 
              slow_imbalance < 0 and 
              current_volume >= volume_threshold):
            # Flow momentum building to the downside, confirmed by slow trend
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-momentum-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
