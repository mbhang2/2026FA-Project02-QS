"""
A volatility-adjusted mean reversion strategy with adaptive thresholds.

The previous strategy achieved -0.612 by using a fixed 0.5% deviation threshold
for mean reversion trades. This approach has limitations:

1. Fixed thresholds don't adapt to changing market volatility
2. Mean reversion alone may not capture the signal effectively
3. The strategy doesn't consider the strength of deviations

This strategy improves by:
1. Computing rolling volatility (standard deviation of returns)
2. Scaling the entry threshold dynamically based on volatility
3. Using both deviation magnitude AND direction consistency
4. Only trading when deviation is significant relative to recent volatility
5. Tracking price momentum to filter mean reversion signals

The logic:
- Calculate 20-bar SMA and rolling volatility
- Scale threshold by volatility (higher volatility = wider bands)
- Buy when price deviates below MA by more than (threshold × volatility)
- Sell when price deviates above MA by more than (threshold × volatility)
- Add a momentum filter: only trade if recent bars show weak trend
- Use brackets for automatic stop/target management
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", lookback: int = 20,
                 vol_lookback: int = 10, vol_threshold: float = 2.0,
                 momentum_lookback: int = 5, stop: float = 0.75,
                 target: float = 0.75, size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.vol_lookback = vol_lookback
        self.vol_threshold = vol_threshold
        self.momentum_lookback = momentum_lookback
        self.stop = stop
        self.target = target
        self.size = size
        # Accumulate closes for MA and volatility calculations
        self.closes: list[float] = []
        # Track returns for volatility
        self.returns: list[float] = []

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

        last_bar = history[-1]
        current_price = last_bar.close

        # Accumulate closes
        self.closes.append(current_price)
        
        # Keep only what we need
        if len(self.closes) > self.lookback + self.vol_lookback + 5:
            self.closes = self.closes[-(self.lookback + self.vol_lookback + 5):]

        # Calculate returns for volatility
        if len(self.closes) >= 2:
            ret = (self.closes[-1] - self.closes[-2]) / self.closes[-2]
            self.returns.append(ret)
            if len(self.returns) > self.vol_lookback + 5:
                self.returns = self.returns[-(self.vol_lookback + 5):]

        # Need enough data for moving average
        if len(self.closes) < self.lookback:
            return

        # Calculate simple moving average
        recent_closes = self.closes[-self.lookback:]
        ma = sum(recent_closes) / len(recent_closes)
        
        if ma <= 0:
            return
        
        # Calculate volatility (standard deviation of returns)
        vol = 0.0
        if len(self.returns) >= self.vol_lookback:
            recent_returns = self.returns[-self.vol_lookback:]
            mean_ret = sum(recent_returns) / len(recent_returns)
            variance = sum((r - mean_ret) ** 2 for r in recent_returns) / len(recent_returns)
            vol = math.sqrt(max(variance, 0.0))
        
        # Use volatility to scale threshold, with minimum threshold
        if vol > 0:
            threshold = self.vol_threshold * vol
        else:
            threshold = 0.002  # Fallback minimum threshold
        
        # Calculate deviation from MA
        deviation = (current_price - ma) / ma
        
        # Calculate momentum: compare recent closes to determine trend strength
        momentum = 0.0
        if len(self.closes) >= self.momentum_lookback:
            momentum_closes = self.closes[-self.momentum_lookback:]
            momentum = (momentum_closes[-1] - momentum_closes[0]) / momentum_closes[0]
        
        # Mean reversion with momentum filter:
        # Buy when price is below MA by threshold amount AND momentum is weak/negative
        if deviation < -threshold and momentum < 0.001:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop,
                target_offset=self.target,
                tag="vol-adjusted-long",
            )
        # Sell when price is above MA by threshold amount AND momentum is weak/positive
        elif deviation > threshold and momentum > -0.001:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop,
                target_offset=self.target,
                tag="vol-adjusted-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
