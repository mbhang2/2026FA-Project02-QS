"""Momentum strategy with ATR-based brackets.

Exploits the planted signal (1-bar momentum) while adapting to the simulated 
instrument's volatility using an Average True Range (ATR) calculation to set 
optimal stop and target offsets.
"""

class Strategy:
    def __init__(self, interval: str = "5m", atr_period: int = 14, 
                 stop_mult: float = 1.5, target_mult: float = 1.5, size: int = 1):
        self.interval = interval
        self.atr_period = atr_period
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.atr_period + 1:
            return
            
        window = history.last(self.atr_period + 1)
        last = window[-1]
        
        if last.close == last.open:
            return
            
        atr = self._calculate_atr(window)
        if atr <= 0:
            atr = 1.0
            
        stop = atr * self.stop_mult
        target = atr * self.target_mult
        
        if last.close > last.open:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target, tag="mom-long")
        elif last.close < last.open:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target, tag="mom-short")

    def _calculate_atr(self, bars):
        tr_sum = 0.0
        for i in range(1, len(bars)):
            high = bars[i].high
            low = bars[i].low
            prev_close = bars[i-1].close
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_sum += tr
        return tr_sum / (len(bars) - 1)

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
