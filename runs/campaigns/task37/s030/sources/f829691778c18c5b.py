"""
Analysis of prior trials and strategy refinement.

Trial scores:
- #0 (baseline momentum): -0.6120
- #1 (volatility mean reversion): -0.6379
- #2 (trend-following): +0.0192 (best)
- #3 (hybrid trend+reversion): -0.3422

Trial #2's trend-following achieved the best score (+0.0192) by:
1. Using dual moving averages (fast=5, slow=20) for trend identification
2. Confirming entries with price position relative to fast MA
3. Scaling stops/targets by volatility

Trial #3 regressed (-0.3422) despite adding complexity:
- Mean reversion within trends may be fighting the trend
- Pullback thresholds and trend strength calculations added noise
- Over-parameterization likely caused overfitting

Key insight: The baseline momentum strategy (-0.6120) outperformed the complex
mean reversion attempts. This suggests:
1. Simple trend-following works better than fighting trends
2. Excessive parameterization hurts generalization
3. The data may have genuine directional bias that rewards following it

This strategy simplifies trial #2 by:
1. Removing the unnecessary trend strength calculation
2. Using a cleaner entry logic: enter if price is in the direction of the trend
3. Keeping volatility-adjusted risk management (this worked in #2)
4. Reducing parameters to prevent overfitting
5. Focusing on the core insight: trend-following works

The goal is to capture the essence of what made #2 work (+0.0192) while
eliminating the complexity that made #3 fail (-0.3422).
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
        
        # Scale stop and target by volatility
        vol_adjustment = 1.0 + (vol * self.vol_multiplier)
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment))
        
        # Simplified trend-following logic (what worked in trial #2):
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
