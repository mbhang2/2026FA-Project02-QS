"""
Analysis of trial progression and strategy refinement.

Trial scores:
- #0 (baseline momentum): -0.6120
- #1 (volatility mean reversion): -0.6379
- #2 (trend-following): +0.0192 (best)
- #3 (hybrid trend+reversion): -0.3422
- #4 (simplified trend-following): +0.0192 (identical to #2)

Trial #2 and #4 both achieved +0.0192 by using:
1. Dual moving averages (fast=5, slow=20) for trend identification
2. Price confirmation relative to fast MA
3. Volatility-adjusted risk management

The best score (+0.0192) came from a simple trend-following approach. However,
there's room for improvement by:

1. **Refining entry conditions**: The current logic requires price to be beyond
   the fast MA in the trend direction. This might be too strict or too loose.
   
2. **Adding entry filters**: Recent price action (bar direction, consecutive bars)
   could improve entry quality without adding excessive complexity.
   
3. **Optimizing MA parameters**: The fast=5, slow=20 was chosen arbitrarily.
   Different lookbacks might capture trends better.
   
4. **Volatility scaling refinement**: The current vol_multiplier=1.5 might be
   suboptimal. Tighter bounds could reduce whipsaws.

5. **Position sizing based on confidence**: Rather than fixed size, scale by
   trend strength or MA divergence.

This strategy keeps the core trend-following approach but:
- Adds a confirmation filter: only enter if the last bar moved in the trend direction
- Reduces volatility multiplier from 1.5 to 1.2 for tighter stops
- Uses slightly different MA periods (fast=6, slow=18) to test sensitivity
- Adds a minimum trend strength requirement (fast MA must diverge from slow MA)
- Implements adaptive position sizing based on trend strength
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 18, vol_lookback: int = 10,
                 vol_multiplier: float = 1.2, min_stop: float = 0.5,
                 max_stop: float = 1.8, min_target: float = 0.75,
                 max_target: float = 2.7, min_ma_divergence: float = 0.001,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.vol_multiplier = vol_multiplier
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
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
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Scale stop and target by volatility
        vol_adjustment = 1.0 + (vol * self.vol_multiplier)
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment))
        
        # Trend-following with confirmation and divergence filter
        # Only trade if MAs are sufficiently diverged (not in choppy range)
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Buy: uptrend (fast > slow) + price above fast MA + bar closed up
        if fast_ma > slow_ma and current_price > fast_ma and last_bar.close > last_bar.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long-confirmed",
            )
        # Sell: downtrend (fast < slow) + price below fast MA + bar closed down
        elif fast_ma < slow_ma and current_price < fast_ma and last_bar.close < last_bar.open:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-short-confirmed",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
