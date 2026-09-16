"""Simple Moving Average Crossover Strategy.

Designed to capture momentum/trend signals using a fast and slow SMA crossover.
It waits for a crossover event to trigger a market order with a bracket for 
protection and profit taking.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        fast_period: int = 5,
        slow_period: int = 20,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
    ):
        self.interval = interval
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.slow_period + 1:
            return

        # We need the last `slow_period + 1` bars to calculate the current and previous SMAs
        window = history.last(self.slow_period + 1)

        # Fast SMA (previous and current)
        fast_window = window[-self.fast_period - 1:]
        fast_sma_prev = sum(b.close for b in fast_window[:-1]) / self.fast_period
        fast_sma_curr = sum(b.close for b in fast_window[1:]) / self.fast_period

        # Slow SMA (previous and current)
        slow_sma_prev = sum(b.close for b in window[:-1]) / self.slow_period
        slow_sma_curr = sum(b.close for b in window[1:]) / self.slow_period

        bullish_cross = fast_sma_prev <= slow_sma_prev and fast_sma_curr > slow_sma_curr
        bearish_cross = fast_sma_prev >= slow_sma_prev and fast_sma_curr < slow_sma_curr

        if bullish_cross:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="sma-crossover-long",
            )
        elif bearish_cross:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="sma-crossover-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
