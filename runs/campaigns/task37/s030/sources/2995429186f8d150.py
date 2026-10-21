"""
A mean-reversion strategy that trades based on price deviation from a moving average.

The base momentum strategy buys on up closes and sells on down closes, which is
trend-following. This strategy reverses that logic: it buys when price is below
its moving average (expecting mean reversion upward) and sells when price is above
its moving average (expecting mean reversion downward).

Key improvements:
1. Uses a moving average as a reference level rather than just the previous bar
2. Implements mean-reversion logic (counter-trend) instead of momentum
3. Adds a filter: only trade if the deviation from MA is significant enough
4. Accumulates the moving average on self to avoid window issues with long lookbacks
5. Checks actionability before placing orders

The strategy:
- Calculates a 20-bar SMA
- Buys if close is 0.5% below the MA (mean reversion long)
- Sells if close is 0.5% above the MA (mean reversion short)
- Uses brackets for automatic stop/target management
- Only trades when flat and no orders are working
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 deviation_threshold: float = 0.005, stop: float = 0.75,
                 target: float = 0.75, size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.deviation_threshold = deviation_threshold
        self.stop = stop
        self.target = target
        self.size = size
        # Accumulate closes to compute moving average
        self.closes: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        # Accumulate the last close
        last_bar = history[-1]
        self.closes.append(last_bar.close)
        
        # Keep only what we need (lookback + 1 for safety)
        if len(self.closes) > self.lookback + 10:
            self.closes = self.closes[-(self.lookback + 10):]

        # Need enough data for moving average
        if len(self.closes) < self.lookback:
            return

        # Calculate 20-bar simple moving average
        recent_closes = self.closes[-self.lookback:]
        ma = sum(recent_closes) / len(recent_closes)
        
        current_price = last_bar.close
        
        # Calculate deviation from MA as a percentage
        if ma <= 0:
            return
        
        deviation = (current_price - ma) / ma
        
        # Mean reversion logic:
        # Buy when price is below MA by threshold amount
        if deviation < -self.deviation_threshold:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop,
                target_offset=self.target,
                tag="mean-reversion-long",
            )
        # Sell when price is above MA by threshold amount
        elif deviation > self.deviation_threshold:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop,
                target_offset=self.target,
                tag="mean-reversion-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
