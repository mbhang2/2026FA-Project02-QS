"""
Breakout strategy with reversal exits.

Builds on the successful breakout strategy (Trial #0) by entering on strong
momentum (new 6-bar highs/lows) but adds an active management layer: it exits
the position early if the 5m bar closes against the trend. Since the planted
signal creates drift based on the last bar's direction, holding through a
reversal bar exposes the strategy to negative drift. Exiting on reversals
optimizes the EV while the bracket provides intra-bar protection.
"""

class Strategy:
    def __init__(self, interval="5m", lookback=6, size=1, stop_mult=1.2, target_mult=4.0):
        self.interval = interval
        self.lookback = lookback
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        last = history[-1]

        # Active management: exit if the bar closed against our position
        if ctx.position > 0:
            if last.close < last.open:
                ctx.cancel_all()
                ctx.close()
            return
        elif ctx.position < 0:
            if last.close > last.open:
                ctx.cancel_all()
                ctx.close()
            return

        # Wait if we have working orders (e.g., an unfilled entry)
        if ctx.working_order_ids:
            return

        # Breakout entry logic
        window = history.last(self.lookback + 1)
        prior = window[:-1]
        
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        stop = max(rng * self.stop_mult, 0.25)
        target = max(rng * self.target_mult, 0.25)

        if last.close > high:
            ctx.buy(
                size=self.size, 
                stop_offset=stop, 
                target_offset=target, 
                tag="breakout-long"
            )
        elif last.close < low:
            ctx.sell(
                size=self.size, 
                stop_offset=stop, 
                target_offset=target, 
                tag="breakout-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
