"""
A trend-following strategy with volatility-adjusted position sizing and dynamic exits.

The previous mean-reversion strategies achieved -0.612 and -0.638, which suggests
that counter-trend trading may not be the right approach for this data. This
strategy reverses direction and implements trend-following instead.

Key insights:
1. The baseline momentum strategy (buy on up closes, sell on down closes) scored
   around -0.61, which is better than mean reversion attempts
2. Mean reversion requires precise thresholds and volatility adjustments, which
   may be overfitting to the training data
3. Trend-following is simpler and more robust across different market regimes
4. Adding adaptive stops based on volatility can improve risk-adjusted returns

This strategy:
- Identifies trends using a combination of moving averages (fast and slow)
- Only enters in the direction of the trend (fast MA > slow MA for longs)
- Uses volatility-adjusted stop and target distances
- Implements a simple trend strength filter to avoid whipsaws
- Accumulates price data efficiently on self
- Exits automatically via brackets on every entry
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 5,
                 slow_lookback: int = 20, vol_lookback: int = 10,
                 vol_multiplier: float = 1.5, min_stop: float = 0.5,
                 max_stop: float = 2.0, min_target: float = 0.75,
                 max_target: float = 3.0, size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.vol_multiplier = vol_multiplier
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
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
        
        # Keep only what we need (slow_lookback + vol_lookback + buffer)
        max_needed = self.slow_lookback + self.vol_lookback + 5
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]

        # Calculate returns for volatility
        if len(self.closes) >= 2:
            prev_price = self.closes[-2]
            if prev_price > 0:
                ret = (current_price - prev_price) / prev_price
                self.returns.append(ret)
                if len(self.returns) > self.vol_lookback + 5:
                    self.returns = self.returns[-(self.vol_lookback + 5):]

        # Need enough data for both moving averages
        if len(self.closes) < self.slow_lookback:
            return

        # Calculate fast and slow moving averages
        fast_closes = self.closes[-self.fast_lookback:]
        slow_closes = self.closes[-self.slow_lookback:]
        
        fast_ma = sum(fast_closes) / len(fast_closes)
        slow_ma = sum(slow_closes) / len(slow_closes)
        
        if slow_ma <= 0:
            return
        
        # Calculate volatility (standard deviation of recent returns)
        vol = 0.0
        if len(self.returns) >= self.vol_lookback:
            recent_returns = self.returns[-self.vol_lookback:]
            mean_ret = sum(recent_returns) / len(recent_returns)
            variance = sum((r - mean_ret) ** 2 for r in recent_returns) / len(recent_returns)
            vol = math.sqrt(max(variance, 0.0))
        
        # Scale stop and target by volatility
        # Higher volatility = wider stops and targets
        vol_adjustment = 1.0 + (vol * self.vol_multiplier)
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment))
        
        # Trend-following logic:
        # Buy when fast MA is above slow MA (uptrend) and price is above fast MA
        if fast_ma > slow_ma and current_price > fast_ma:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell when fast MA is below slow MA (downtrend) and price is below fast MA
        elif fast_ma < slow_ma and current_price < fast_ma:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
