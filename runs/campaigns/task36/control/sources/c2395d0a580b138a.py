"""Mean Reversion Strategy based on Simple RSI.

Reasoning:
Given that the dataset is a simulated random walk, true momentum or trend-following 
edges are unlikely to be present. Mean reversion might capitalize on short-term 
price extremes that revert to the mean. This strategy calculates a simple Relative 
Strength Index (RSI) over a rolling window of closed bars. It initiates a long 
position when the RSI falls below a specified lower threshold (oversold) and a 
short position when the RSI rises above a higher threshold (overbought). Exits 
are managed entirely by the engine via OCO brackets (stop loss and take profit) 
defined by fixed offsets.
"""

class Strategy:
    def __init__(
        self, 
        interval: str = "5m", 
        window: int = 14, 
        rsi_low: float = 30.0, 
        rsi_high: float = 70.0, 
        size: int = 1, 
        stop_offset: float = 0.75, 
        target_offset: float = 0.75
    ):
        self.interval = interval
        self.window = window
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Wait for actionable state and ensure we only hold one position at a time
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        # We need `window + 1` bars to calculate `window` periods of changes
        if len(history) < self.window + 1:
            return
            
        window_bars = history.last(self.window + 1)
        gains = 0.0
        losses = 0.0
        
        for i in range(1, len(window_bars)):
            change = window_bars[i].close - window_bars[i-1].close
            if change > 0:
                gains += change
            else:
                losses -= change
                
        total_change = gains + losses
        if total_change == 0:
            return
            
        rsi = 100.0 * gains / total_change
        
        if rsi < self.rsi_low:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="rsi-long"
            )
        elif rsi > self.rsi_high:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="rsi-short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
