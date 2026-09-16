"""
Momentum strategy comparing the current close to the close N bars ago,
with dynamic brackets based on recent volatility (ATR proxy).

The baseline strategy suggests the planted signal is a simple momentum effect.
This strategy generalizes that by allowing a configurable lookback for the
momentum comparison, and scales the stop loss and take profit dynamically
using the Average True Range (ATR) to adapt to the random walk's volatility.
"""

class Strategy:
    def __init__(
        self,
        interval="5m",
        lookback=3,
        atr_period=14,
        size=1,
        stop_mult=1.5,
        target_mult=3.0
    ):
        self.interval = interval
        self.lookback = lookback
        self.atr_period = atr_period
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        needed = max(self.lookback, self.atr_period) + 1
        if len(history) < needed:
            return

        window = history.last(needed)
        last = window[-1]
        past_for_mom = window[-(self.lookback + 1)]
        
        # Calculate ATR proxy (Average True Range over atr_period)
        atr_window = window[-(self.atr_period + 1):]
        tr_sum = 0.0
        for i in range(1, len(atr_window)):
            current = atr_window[i]
            previous = atr_window[i-1]
            tr = max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close)
            )
            tr_sum += tr
        atr = tr_sum / self.atr_period

        if atr <= 0:
            return

        stop = atr * self.stop_mult
        target = atr * self.target_mult

        if last.close > past_for_mom.close:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="mom-long"
            )
        elif last.close < past_for_mom.close:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="mom-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
