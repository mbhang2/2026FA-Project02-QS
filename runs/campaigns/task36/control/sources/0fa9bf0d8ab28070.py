"""
A momentum strategy that buys if the last bar closed up and sells if it closed down,
capitalizing on the planted positive autocorrelation signal.
It uses an Average True Range (ATR) based bracket for adaptive risk management,
ensuring targets and stops scale with the instrument's volatility to overcome commissions.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=14, size=1, stop_mult=1.5, target_mult=3.0):
        self.interval = interval
        self.lookback = lookback
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback:
            return

        window = history.last(self.lookback)
        
        tr_sum = 0.0
        for i in range(1, len(window)):
            prev = window[i-1]
            curr = window[i]
            tr = max(curr.high - curr.low, abs(curr.high - prev.close), abs(curr.low - prev.close))
            tr_sum += tr
            
        atr = tr_sum / (len(window) - 1)
        if atr <= 0:
            atr = 1.0

        stop_offset = atr * self.stop_mult
        target_offset = atr * self.target_mult

        last = window[-1]
        
        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="momentum-long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="momentum-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
