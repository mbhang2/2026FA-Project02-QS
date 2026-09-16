"""Mean reversion strategy using Bollinger Bands.

Computes a 20-bar SMA and standard deviation. Enters long when the close crosses
below the lower band, and short when it crosses above the upper band. Uses a
bracket sized dynamically based on the recent standard deviation to manage the
trade.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 20,
        num_std: float = 2.0,
        size: int = 1,
        stop_mult: float = 1.5,
        target_mult: float = 1.5,
    ):
        self.interval = interval
        self.lookback = lookback
        self.num_std = num_std
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback:
            return

        window = history.last(self.lookback)
        closes = [b.close for b in window]
        sma = sum(closes) / self.lookback
        variance = sum((c - sma) ** 2 for c in closes) / self.lookback
        std = variance ** 0.5

        if std <= 0:
            return

        last = window[-1]

        stop_dist = max(0.25, std * self.stop_mult)
        target_dist = max(0.25, std * self.target_mult)

        if last.close < sma - self.num_std * std:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="mr-long",
            )
        elif last.close > sma + self.num_std * std:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="mr-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
