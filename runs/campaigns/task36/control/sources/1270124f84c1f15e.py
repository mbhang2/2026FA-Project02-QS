"""Momentum strategy with an asymmetric risk-reward bracket.

This strategy follows the planted momentum signal (buying after an up interval,
selling after a down interval) but improves upon the symmetric 0.75/0.75 bracket
by using an asymmetric 1.0 stop and 2.0 target. This allows winning trades to 
run further, capturing more of the planted trend extension while keeping the 
position strictly protected.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", stop_offset: float = 1.0, 
                 target_offset: float = 2.0, size: int = 1):
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
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
        
        if last.close > last.open:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
