"""
Analysis of trial progression and optimization strategy.

Trial scores progression:
- #0-#4: Negative to near-zero (baseline, volatility, trend attempts)
- #5: +0.3776 (MA divergence + bar confirmation + vol scaling)
- #6: +0.3976 (relaxed MA entry condition)
- #7: +0.8011 (best so far - increased min_ma_divergence to 0.0015)
- #8: +0.7282 (bar strength filter was too restrictive)
- #9: +0.7305 (stricter price > fast_ma condition)
- #10: +0.9545 (best so far - adjusted slow_lookback=20, vol_lookback=13, min_target=0.9)

Trial #10 achieved +0.9545, a significant improvement over #7's +0.8011.
Key success factors in #10:
1. slow_lookback=20 (longer, more stable trend identification)
2. vol_lookback=13 (more stable volatility estimates)
3. min_target=0.9 (slightly wider minimum target)
4. min_ma_divergence=0.0015 (filters choppy trades)
5. vol_multiplier=1.2 (good risk/reward scaling)
6. Entry condition: current_price >= fast_ma (inclusive, catches more trends)

The improvement from #7 to #10 suggests:
- Longer slow MA period (20 vs 19) helps identify trends more reliably
- More stable volatility window (13 vs 12) improves risk sizing consistency
- Slightly wider minimum target (0.9 vs 0.85) improves risk/reward

Optimization strategy for next trial (#11):
Fine-tune around the winning configuration (#10) by exploring:

1. Fast MA period: #10 used fast=5, try fast=6
   - Slightly longer fast MA might reduce whipsaws in choppy markets
   - Still responsive enough to capture trend changes

2. Volatility multiplier: #10 used vol_multiplier=1.2, try vol_multiplier=1.25
   - Slightly higher multiplier scales position sizing more aggressively
   - Could improve returns on volatile moves while maintaining risk control

3. Min MA divergence: #10 used 0.0015, try 0.00145
   - Slightly lower threshold catches weaker trends
   - Fine-tuning the sweet spot for trend detection

4. Min stop: #10 used 0.65, try 0.68
   - Slightly wider minimum stop might reduce whipsaws
   - Still maintains tight risk control

5. Max target: #10 used 2.5, try 2.6
   - Slightly wider maximum target allows for bigger wins
   - Complements the wider min_target adjustment

This strategy attempts to improve on #10 by:
- Increasing fast_lookback from 5 to 6 (slightly more stable fast MA)
- Increasing vol_multiplier from 1.2 to 1.25 (more aggressive position sizing)
- Keeping slow_lookback=20 (proven optimal)
- Keeping vol_lookback=13 (proven optimal)
- Keeping min_ma_divergence=0.0015 (proven optimal)
- Keeping min_target=0.9 (proven optimal)
- Slightly increasing min_stop from 0.65 to 0.68 (reduce whipsaws)
- Slightly increasing max_target from 2.5 to 2.6 (allow bigger wins)
- Keeping entry condition as >= (proven to work well)
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 13,
                 vol_multiplier: float = 1.25, min_stop: float = 0.68,
                 max_stop: float = 1.5, min_target: float = 0.9,
                 max_target: float = 2.6, min_ma_divergence: float = 0.0015,
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
