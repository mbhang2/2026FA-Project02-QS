"""Bollinger Band mean reversion strategy with volatility-adjusted brackets.

Fades moves that stretch beyond a standard deviation band from a simple moving average.
Stop and target offsets are scaled by the recent standard deviation to adapt to the
price scale of the synthetic random walk.
"""

class Strategy:
    def __init__(self, interval="1m", lookback=20, std_dev=1.5, size=1,
                 stop_mult=1.5, target_mult=1.5):
        self.interval = interval
        self.lookback = lookback
        self.std_dev = std_dev
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
        closes = [b.close for b in window]
        mean = sum(closes) / self.lookback
        variance = sum((c - mean) ** 2 for c in closes) / self.lookback
        std = variance ** 0.5
        
        if std <= 0:
            return

        last_close = closes[-1]
        upper_band = mean + self.std_dev * std
        lower_band = mean - self.std_dev * std

        stop_dist = self.stop_mult * std
        target_dist = self.target_mult * std

        if last_close > upper_band:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="fade-up"
            )
        elif last_close < lower_band:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="fade-down"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
