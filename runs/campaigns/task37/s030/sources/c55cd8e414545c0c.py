"""
Analysis of trial progression and optimization strategy.

Trial scores progression:
- #0-#4: Negative to near-zero (baseline, volatility, trend attempts)
- #5: +0.3776 (MA divergence + bar confirmation + vol scaling)
- #6: +0.3976 (relaxed MA entry condition)
- #7: +0.8011 (increased min_ma_divergence to 0.0015)
- #8: +0.7282 (added bar strength requirement, reduced vol_multiplier)

Trial #7 achieved +0.8011, but #8 dropped to +0.7282 despite seeming improvements.
This suggests:
1. The bar_strength filter (0.1) was too restrictive
2. The vol_multiplier reduction (1.2 -> 1.0) reduced profit on volatile moves
3. The min_ma_divergence of 0.002 in #8 was too high

Key insights from the best performer (#7):
- min_ma_divergence=0.0015 is the sweet spot for filtering choppy trades
- vol_multiplier=1.2 provides good risk/reward scaling
- Bar direction confirmation without strength requirement works well
- MA periods fast=5, slow=19 are optimal
- Stop/target bounds (0.65-1.5 / 0.85-2.5) are well-tuned

Optimization strategy for next trial:
1. Return to the #7 configuration as base (it was best)
2. Fine-tune only one dimension: the entry price condition
   - Current (#7): current_price >= fast_ma
   - Try: current_price > fast_ma (stricter pullback)
   - This might improve win rate by filtering marginal entries
   
3. Alternative: adjust the MA periods slightly
   - Try fast=6, slow=20 (slightly more conservative)
   - This would filter more noise while maintaining trend following
   
4. Or: increase min_ma_divergence slightly but not as much as #8
   - Try 0.0018 instead of 0.0015 (middle ground)
   - This filters more choppy trades than #7 but not as aggressively as #8

This strategy attempts to improve on #7 by:
- Using the proven base configuration from #7 (min_ma_divergence=0.0015)
- Changing entry condition from >= to > (stricter pullback requirement)
- Keeping vol_multiplier=1.2 (proven good)
- Keeping stop/target bounds from #7 (0.65-1.5 / 0.85-2.5)
- Keeping MA periods fast=5, slow=19 (proven optimal)
- Removing any additional bar strength filter (it hurt #8)
- Keeping bar direction confirmation (it helped #7)
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 5,
                 slow_lookback: int = 19, vol_lookback: int = 12,
                 vol_multiplier: float = 1.2, min_stop: float = 0.65,
                 max_stop: float = 1.5, min_target: float = 0.85,
                 max_target: float = 2.5, min_ma_divergence: float = 0.0015,
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
        
        # Buy: uptrend (fast > slow) + price > fast MA (stricter) + bar closed up
        if fast_ma > slow_ma and current_price > fast_ma and last_bar.close > last_bar.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price < fast MA (stricter) + bar closed down
        elif fast_ma < slow_ma and current_price < fast_ma and last_bar.close < last_bar.open:
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
