"""
Analysis of trial progression and optimization strategy.

Trial scores:
- #0 (baseline momentum): -0.6120
- #1 (volatility mean reversion): -0.6379
- #2 (trend-following): +0.0192
- #3 (hybrid trend+reversion): -0.3422
- #4 (simplified trend-following): +0.0192
- #5 (trend with confirmation): +0.3776 (best so far)

Trial #5 achieved +0.3776 by adding:
1. MA divergence filter to avoid choppy ranges
2. Bar direction confirmation (close > open for buys, close < open for sells)
3. Tighter volatility scaling (vol_multiplier=1.2)
4. Adjusted MA periods (fast=6, slow=18)

Key observations:
1. The bar direction confirmation filter was crucial - it filters out weak entries
2. The MA divergence requirement prevents trading in choppy, range-bound markets
3. Volatility scaling helps adapt to market conditions

Further optimization opportunities:
1. The current approach requires price to be beyond fast MA. This might be too strict.
   - Consider allowing entry at the fast MA level instead of beyond it
   - This would increase entry frequency while maintaining quality filters

2. The bar direction confirmation is powerful but might be filtering too aggressively.
   - Consider requiring only that the bar closed in the trend direction
   - OR add a secondary filter: bar closed beyond the open by a minimum amount

3. Volatility multiplier and bounds might still be suboptimal:
   - Current: vol_multiplier=1.2, min_stop=0.5, max_stop=1.8
   - The min_stop of 0.5 might be too tight (whipsaws)
   - The max_stop of 1.8 might be too loose (excessive risk)

4. MA period optimization:
   - fast=6, slow=18 is close to the original 5/20
   - Try slightly tighter periods: fast=4, slow=16 for faster trend detection
   - OR slightly looser: fast=8, slow=24 for more stable trends

5. Entry logic refinement:
   - Current: price must be beyond fast MA in trend direction
   - Alternative: price just needs to be on the correct side of fast MA
   - This would increase entries without sacrificing the confirmation filter

This strategy:
- Relaxes the price vs fast MA requirement (allows entry at MA level, not beyond)
- Keeps the bar direction confirmation (powerful filter)
- Keeps the MA divergence requirement (avoids choppy markets)
- Adjusts MA periods to fast=5, slow=19 (between trials #2 and #5)
- Slightly increases min_stop to 0.6 to reduce whipsaws
- Reduces max_stop to 1.6 to limit risk on high volatility
- Keeps volatility multiplier at 1.2 (worked well in #5)
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 5,
                 slow_lookback: int = 19, vol_lookback: int = 10,
                 vol_multiplier: float = 1.2, min_stop: float = 0.6,
                 max_stop: float = 1.6, min_target: float = 0.75,
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
        
        # Buy: uptrend (fast > slow) + price >= fast MA + bar closed up
        if fast_ma > slow_ma and current_price >= fast_ma and last_bar.close > last_bar.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + bar closed down
        elif fast_ma < slow_ma and current_price <= fast_ma and last_bar.close < last_bar.open:
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
