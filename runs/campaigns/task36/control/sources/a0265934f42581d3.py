class Strategy:
    """Continuous momentum strategy.
    
    Reverses position based on the direction of the last closed bar.
    By staying in the market and not using tight brackets, it captures 
    longer trends and avoids being chopped out by intra-bar noise, 
    while also saving commissions on consecutive bars in the same direction.
    """

    def __init__(self, interval: str = "5m", size: int = 1):
        self.interval = interval
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
            
        # Wait for any pending orders to fill before issuing new ones
        if ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        # Determine target position based on the last bar's direction
        if last.close > last.open:
            target_pos = self.size
        elif last.close < last.open:
            target_pos = -self.size
        else:
            target_pos = ctx.position
            
        current_pos = ctx.position
        
        # Execute the required delta
        if target_pos > current_pos:
            ctx.buy(size=target_pos - current_pos, tag="reverse_long")
        elif target_pos < current_pos:
            ctx.sell(size=current_pos - target_pos, tag="reverse_short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
