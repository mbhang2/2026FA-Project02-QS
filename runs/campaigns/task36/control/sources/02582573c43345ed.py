"""
Continuous SAR (Stop And Reverse) momentum strategy.

Capitalizes on the planted momentum signal by holding a position in the direction 
of the last closed bar. By continuously reversing the position instead of exiting 
to flat, it saves on transaction costs and maximizes time-in-market. It avoids 
using brackets to ensure the position captures the full magnitude of the bar's move.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", size: int = 1):
        self.interval = interval
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Wait for any pending market orders to fill
        if not ctx.actionable or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        # Determine the target position based on the momentum of the last closed bar
        if last.close > last.open:
            signal = 1
        elif last.close < last.open:
            signal = -1
        else:
            signal = 0
            
        target_position = signal * self.size
        current_position = ctx.position
        
        # Adjust position to match the target
        if current_position != target_position:
            diff = target_position - current_position
            if diff > 0:
                ctx.buy(size=diff, tag="long")
            elif diff < 0:
                ctx.sell(size=-diff, tag="short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
