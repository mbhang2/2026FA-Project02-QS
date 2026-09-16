"""Continuous momentum strategy.

Exploits the planted 1-bar continuation signal by staying in the market
continuously in the direction of the last closed bar. By reversing position
when the signal changes rather than using brackets, it avoids time spent flat
and captures the full move. It acts only on bar closes, naturally controlling
turnover and commission costs.
"""

class Strategy:
    def __init__(self, interval: str = "5m", size: int = 1):
        self.interval = interval
        self.size = size

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        if last.close == last.open:
            return
            
        # The planted signal: next interval follows the last one's direction
        desired_position = self.size if last.close > last.open else -self.size
        
        if ctx.position == desired_position:
            return
            
        # Clear any working orders (though market orders should fill immediately)
        for oid in ctx.working_order_ids:
            ctx.cancel(oid)
            
        trade_qty = desired_position - ctx.position
        
        if trade_qty > 0:
            ctx.buy(size=trade_qty, tag="reverse-to-long")
        elif trade_qty < 0:
            ctx.sell(size=-trade_qty, tag="reverse-to-short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
