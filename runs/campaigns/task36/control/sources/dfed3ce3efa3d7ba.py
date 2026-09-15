"""
Mean reversion strategy using Bollinger Bands.
Since the dataset is a simulated random walk, there may be no true edge,
but mean reversion can sometimes capture short-term structural mean reversion 
(e.g., bid-ask bounce or discrete tick effects) if any exist, or simply 
serve as a robust baseline that avoids trend-following losses in noise.
We use 5m bars, a 20-bar window, and 2 standard deviations.
Orders are protected by bracket offsets.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        window: int = 20,
        num_std: float = 2.0,
        size: int = 1,
        stop_offset: float = 2.0,
        target_offset: float = 2.0,
    ):
        self.interval = interval
        self.window = window
        self.num_std = num_std
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.window:
            return

        window_bars = history.last(self.window)
        closes = [b.close for b in window_bars]

        mean = sum(closes) / self.window
        variance = sum((c - mean) ** 2 for c in closes) / self.window
        std = variance ** 0.5

        if std == 0:
            return

        upper = mean + self.num_std * std
        lower = mean - self.num_std * std

        last_close = closes[-1]

        if last_close > upper:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-short",
            )
        elif last_close < lower:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="bb-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
