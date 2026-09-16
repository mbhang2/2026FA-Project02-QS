"""Momentum strategy with an asymmetric bracket to capture trend continuation.

The dataset contains a planted momentum signal (positive autocorrelation). 
The baseline strategy uses a symmetric 0.75/0.75 bracket. By using an asymmetric 
bracket with a larger target (1.5) and the same stop (0.75), we allow the 
planted trend to run further, improving the risk-reward ratio while keeping 
the same entry logic.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", stop: float = 0.75,
                 target: float = 1.5, size: int = 1):
        self.interval = interval
        self.stop = stop
        self.target = target
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        if last.close == last.open:
            return
            
        place = ctx.buy if last.close > last.open else ctx.sell
        place(
            size=self.size,
            stop_offset=self.stop,
            target_offset=self.target,
            tag="entry",
        )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
