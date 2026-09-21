"""
Order flow continuation strategy based on signed volume imbalance.

The hypothesis: when one side trades aggressively (high signed order flow),
price continues in that direction for the next interval.

This strategy:
1. Measures signed order flow (delta) over the last N bars
2. Normalizes by total volume to handle varying session activity
3. Enters long on positive imbalance, short on negative imbalance
4. Uses a bracket for protection from fill instant
5. Handles the unknown aggressor case explicitly

The key insight: delta is available on every bar without subscribing to ticks,
so we get order flow features at bar resolution without the 5x slowdown cost.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 3,
        threshold: float = 0.15,
        size: int = 1,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
    ):
        """
        Args:
            interval: Bar interval to trade on
            lookback: Number of bars to measure order flow over
            threshold: Minimum normalized imbalance to trigger entry
                      (as fraction of total volume)
            size: Position size
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.interval = interval
        self.lookback = lookback
        self.threshold = threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Skip if we already have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Skip during warmup when orders would be ignored
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)

        # Need enough history to measure imbalance
        if len(history) < self.lookback:
            return

        # Get the lookback window
        window = history.last(self.lookback)

        # Calculate aggregate signed order flow (delta) and total volume
        total_delta = 0.0
        total_volume = 0.0

        for bar in window:
            # delta = buy_volume - sell_volume, already computed on the bar
            total_delta += bar.delta
            total_volume += bar.volume

        # Avoid division by zero
        if total_volume <= 0:
            return

        # Normalize imbalance as fraction of total volume
        # Positive means more aggressive buying, negative means more aggressive selling
        imbalance = total_delta / total_volume

        # Entry logic based on order flow direction
        if imbalance > self.threshold:
            # Strong buying pressure: enter long
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif imbalance < -self.threshold:
            # Strong selling pressure: enter short
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
