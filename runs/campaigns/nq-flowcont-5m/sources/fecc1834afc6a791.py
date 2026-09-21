"""
Flow continuation strategy with adaptive thresholding and multi-timeframe confirmation.

The prior trial scored -0.6012 with a fixed 0.30 threshold. This iteration explores:

1. Adaptive threshold based on recent volatility/volume - normalize against session context
2. Multi-bar confirmation: require flow signal to persist across 2 consecutive bars
3. Tighter, asymmetric brackets: smaller target for fades, larger for continuation
4. Filter out low-volume bars that may have outsized delta due to noise
5. Track session-level statistics to adapt entry aggressiveness

Key changes from #0:
- Entry only when last 2 bars both show directional flow agreement (reduces noise)
- Threshold adapts to recent volume profile (avoid over-trading quiet periods)
- Minimum volume filter to avoid thin bars
- Tighter targets (1.0) but asymmetric stops (1.5) to favor continuation direction
- Smaller position size as baseline to leave room for scaling
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 10,
        min_volume: int = 100,
        flow_threshold: float = 0.25,
        stop_offset: float = 1.5,
        target_offset: float = 1.0,
        size: int = 1,
        confirmation_bars: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign)
            lookback: Bars to track for adaptive threshold
            min_volume: Minimum volume to avoid thin-bar noise
            flow_threshold: Base normalized delta threshold
            stop_offset: Distance for protective stop
            target_offset: Distance for profit target
            size: Position size in contracts
            confirmation_bars: Number of consecutive bars with same flow direction required
        """
        self.interval = interval
        self.lookback = lookback
        self.min_volume = min_volume
        self.flow_threshold = flow_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.confirmation_bars = confirmation_bars
        
        # Track recent deltas for adaptive threshold
        self._recent_deltas: deque = deque(maxlen=lookback)
        self._recent_volumes: deque = deque(maxlen=lookback)

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic with multi-bar confirmation and adaptive thresholding.
        """
        if not ctx.actionable:
            return
        
        if ctx.position or ctx.working_order_ids:
            return
        
        history = ctx.history(self.interval)
        if len(history) < self.confirmation_bars:
            return
        
        # Update tracking of recent bars
        last_bar = history[-1]
        self._recent_deltas.append(last_bar.delta)
        self._recent_volumes.append(last_bar.volume)
        
        # Need at least confirmation_bars to check consensus
        if len(self._recent_deltas) < self.confirmation_bars:
            return
        
        # Filter out low-volume bars
        if last_bar.volume < self.min_volume:
            return
        
        # Adaptive threshold based on recent volume
        avg_volume = sum(self._recent_volumes) / len(self._recent_volumes)
        if avg_volume <= 0:
            return
        
        # Check multi-bar confirmation: last N bars all show same directional flow
        recent_window = list(self._recent_deltas)[-self.confirmation_bars:]
        
        # Check if all bars in window agree on direction
        all_positive = all(d > 0 for d in recent_window)
        all_negative = all(d < 0 for d in recent_window)
        
        if not (all_positive or all_negative):
            return
        
        # Compute normalized delta for the current bar
        total_volume = last_bar.volume
        if total_volume <= 0:
            return
        
        delta = last_bar.delta
        normalized_delta = delta / total_volume
        
        # Adaptive threshold: tighter when volume is high (more reliable signal)
        volume_factor = min(total_volume / avg_volume, 2.0)  # Cap at 2x
        adaptive_threshold = self.flow_threshold / volume_factor
        
        # Entry signal: strong directional flow with multi-bar confirmation
        if normalized_delta > adaptive_threshold and all_positive:
            # Strong buy-side aggression: enter long
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long-confirmed",
            )
            
        elif normalized_delta < -adaptive_threshold and all_negative:
            # Strong sell-side aggression: enter short
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short-confirmed",
            )

    def on_fill(self, ctx) -> None:
        """
        Position filled. Bracket handles the exit.
        """
        pass

    def on_lifecycle(self, ctx) -> None:
        """
        Track lifecycle events.
        """
        pass
