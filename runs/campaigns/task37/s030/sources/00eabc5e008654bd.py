"""
Analysis of trial progression and next optimization.

Trial scores:
- #0 (baseline momentum): -0.6120
- #1 (volatility mean reversion): -0.6379
- #2 (trend-following): +0.0192
- #3 (hybrid trend+reversion): -0.3422
- #4 (simplified trend-following): +0.0192
- #5 (trend with confirmation): +0.3776
- #6 (best so far): +0.3976

Trial #6 achieved +0.3976 by:
1. Relaxing the price vs fast MA requirement (>= instead of >)
2. Keeping bar direction confirmation
3. Keeping MA divergence filter to avoid choppy markets
4. Using MA periods fast=5, slow=19
5. Adjusted stop/target bounds: min_stop=0.6, max_stop=1.6

Key insights from the progression:
1. Bar direction confirmation is a crucial filter
2. MA divergence requirement prevents choppy-market losses
3. The relaxation from > to >= on fast MA improved results
4. Volatility scaling with vol_multiplier=1.2 works well
5. Tighter stop bounds (0.6-1.6) outperform looser ones (0.5-1.8)

Further optimization opportunities:
1. The min_ma_divergence=0.001 (0.1%) is very tight. Slightly higher threshold
   might filter more choppy trades while keeping good trends.
   
2. The volatility calculation uses log returns. Consider:
   - Increasing vol_lookback slightly (10 -> 12) for more stable vol estimate
   - OR decreasing it (10 -> 8) for faster adaptation to volatility spikes
   
3. Entry confirmation: currently requires bar to close in trend direction.
   - Could add: require bar to move by minimum amount (e.g., 0.25 price units)
   - This would filter weak bars while keeping the direction confirmation
   
4. Target offset scaling: currently scales with volatility like stops do.
   - Could try: min_target=0.9, max_target=2.5 (slightly tighter)
   - This might improve win rate without sacrificing big winners
   
5. MA period fine-tuning: fast=5, slow=19 is good, but:
   - Try fast=5, slow=20 (original tested period) for comparison
   - OR fast=6, slow=18 (from trial #5) which also scored well
   
6. Consider adding a secondary filter on recent volatility:
   - Only trade if current volatility is not at extreme highs
   - This might reduce losses during volatile shakeouts

This strategy attempts to improve on #6 by:
- Increasing min_ma_divergence from 0.001 to 0.0015 (filters more choppy trades)
- Increasing vol_lookback from 10 to 12 (more stable volatility estimates)
- Adjusting min_stop from 0.6 to 0.65 (slightly wider stops, reduce whipsaws)
- Adjusting max_stop from 1.6 to 1.5 (tighter cap on risk)
- Adjusting min_target from 0.75 to 0.85 (slightly wider targets)
- Keeping fast=5, slow=19 (the working periods)
- Keeping vol_multiplier=1.2 (works well)
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
