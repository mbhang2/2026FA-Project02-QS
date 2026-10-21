"""
Analysis of trial progression and optimization strategy.

Trial scores progression:
- #0-#4: Negative to near-zero (baseline, volatility, trend attempts)
- #5: +0.3776 (MA divergence + bar confirmation + vol scaling)
- #6: +0.3976 (relaxed MA entry condition)
- #7: +0.8011 (best so far - increased min_ma_divergence to 0.0015)
- #8: +0.7282 (bar strength filter was too restrictive)
- #9: +0.7305 (stricter price > fast_ma condition)

Trial #7 remains the best at +0.8011. Key success factors:
1. min_ma_divergence=0.0015 filters choppy trades effectively
2. vol_multiplier=1.2 provides good risk/reward scaling
3. Bar direction confirmation without strength filter
4. MA periods fast=5, slow=19 are optimal
5. Stop/target bounds (0.65-1.5 / 0.85-2.5) are well-tuned

Trial #9 (score +0.7305) added stricter entry (price > fast_ma instead of >=)
but this reduced the score by ~0.07, suggesting the stricter condition filters
out some profitable trades.

Optimization strategy for next trial:
The best approach is to fine-tune around the winning configuration (#7) by
adjusting parameters that haven't been fully explored:

1. MA period optimization: #7 used fast=5, slow=19
   - Try fast=5, slow=20 (slightly longer slow MA for more stable trends)
   - This might reduce false signals while maintaining trend capture
   
2. Volatility window: #7 used vol_lookback=12
   - Try vol_lookback=13 or 14 for slightly more stable vol estimates
   - More stable vol = more consistent stop/target sizing
   
3. Min MA divergence: #7 used 0.0015 (0.15%)
   - Try 0.00145 (slightly lower) to catch slightly weaker trends
   - This is a fine-tuning of the sweet spot
   
4. Stop/target scaling: #7 used min_stop=0.65, max_stop=1.5, min_target=0.85, max_target=2.5
   - Try adjusting min_target from 0.85 to 0.9 (slightly wider minimum target)
   - This might improve risk/reward ratio
   
5. Entry price condition: Return to >= (from #7) which outperformed > (from #9)
   - The >= condition allowed more entries and #7 scored better than #9

This strategy attempts to improve on #7 by:
- Keeping the proven base: min_ma_divergence=0.0015, vol_multiplier=1.2
- Adjusting slow MA period from 19 to 20 (longer, more stable trend identification)
- Increasing vol_lookback from 12 to 13 (more stable volatility estimates)
- Adjusting min_target from 0.85 to 0.9 (slightly wider minimum target)
- Keeping entry condition as >= (which worked better than > in #9)
- Keeping bar direction confirmation without strength filter
- Keeping fast=5, slow=20 MA periods
- Keeping stop bounds: min_stop=0.65, max_stop=1.5
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 5,
                 slow_lookback: int = 20, vol_lookback: int = 13,
                 vol_multiplier: float = 1.2, min_stop: float = 0.65,
                 max_stop: float = 1.5, min_target: float = 0.9,
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
