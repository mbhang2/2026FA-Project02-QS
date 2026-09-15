"""ATR-based Momentum Strategy

Builds on the baseline 1-bar momentum signal but uses Average True Range (ATR) 
to dynamically size the protective stop and target brackets based on recent volatility,
improving the risk-reward capture over fixed-distance brackets.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", atr_period: int = 14, size: int = 1, 
                 stop_mult: float = 1.0, target_mult: float = 2.0):
        self.interval = interval
        self.atr_period = atr_period
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.atr_period + 1:
            return
            
        window = history.last(self.atr_period + 1)
        
        tr_list = []
        for i in range(1, len(window)):
            prev = window[i-1]
            curr = window[i]
            tr = max(
                curr.high - curr.low, 
                abs(curr.high - prev.close), 
                abs(curr.low - prev.close)
            )
            tr_list.append(tr)
            
        atr = sum(tr_list) / self.atr_period
        if atr <= 0:
            return
            
        last = window[-1]
        
        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=atr * self.stop_mult,
                target_offset=atr * self.target_mult,
                tag="atr-long",
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=atr * self.stop_mult,
                target_offset=atr * self.target_mult,
                tag="atr-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
