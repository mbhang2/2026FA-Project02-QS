"""
A continuous Stop-and-Reverse (SAR) momentum strategy.

The reference strategy uses the planted signal (1-bar momentum) but exits early 
using fixed brackets, missing the rest of the trend. This strategy maximizes 
the edge by holding the position as long as the momentum persists, reversing 
only when the signal flips. It minimizes commission drag by holding through flat bars.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", size: int = 1):
        self.interval = interval
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        # Determine the desired position based on the planted momentum signal
        if last.close > last.open:
            desired = self.size
        elif last.close < last.open:
            desired = -self.size
        else:
            desired = ctx.position  # Maintain position on flat bars to save commissions
            
        current = ctx.position
        
        if desired != current:
            # Clear any working orders to avoid complex state or exceeding limits
            if ctx.working_order_ids:
                ctx.cancel_all()
                return
                
            diff = desired - current
            if diff > 0:
                oid = ctx.buy(size=diff, tag="sar_long")
                if oid is None:
                    return  # Action cap hit
            elif diff < 0:
                oid = ctx.sell(size=abs(diff), tag="sar_short")
                if oid is None:
                    return  # Action cap hit

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
