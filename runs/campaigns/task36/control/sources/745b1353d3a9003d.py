"""
Continuous 1-bar momentum strategy with ATR-based brackets.
Designed to capture the planted 1-bar autocorrelation signal while 
protecting against intra-bar variance using dynamic brackets.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", size: int = 1, 
                 stop_atr: float = 2.0, target_atr: float = 4.0, 
                 atr_period: int = 14):
        self.interval = interval
        self.size = size
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.atr_period = atr_period

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
            
        signal = 0
        if last.close > last.open:
            signal = 1
        elif last.close < last.open:
            signal = -1
            
        if signal == 0:
            return
            
        current_pos = ctx.position
        
        # Hold if we are already in the direction of the signal
        if current_pos != 0:
            if (current_pos > 0 and signal == 1) or (current_pos < 0 and signal == -1):
                return
                
        if ctx.working_order_ids:
            ctx.cancel_all()
            
        if current_pos != 0:
            close_id = ctx.close()
            if close_id is None:
                return
                
        # Calculate ATR
        window = history.last(self.atr_period + 1)
        if len(window) > 1:
            trs = []
            for i in range(1, len(window)):
                curr = window[i]
                prev = window[i-1]
                tr = max(curr.high - curr.low, 
                         abs(curr.high - prev.close), 
                         abs(curr.low - prev.close))
                trs.append(tr)
            atr = sum(trs) / len(trs)
        else:
            atr = last.high - last.low
            
        atr = max(atr, 1e-4)
        
        stop_dist = atr * self.stop_atr
        target_dist = atr * self.target_atr
        
        if signal == 1:
            ctx.buy(size=self.size, stop_offset=stop_dist, target_offset=target_dist, tag="long")
        elif signal == -1:
            ctx.sell(size=self.size, stop_offset=stop_dist, target_offset=target_dist, tag="short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
