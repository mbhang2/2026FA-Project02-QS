"""
Analysis of trial progression and optimization strategy.

Trial scores progression:
- #0-#4: Negative to near-zero (baseline, volatility, trend attempts)
- #5: +0.3776 (MA divergence + bar confirmation + vol scaling)
- #6: +0.3976 (relaxed MA entry condition)
- #7: +0.8011 (best so far - increased min_ma_divergence to 0.0015)

Trial #7 achieved +0.8011 by:
1. Higher MA divergence threshold (0.0015) - filters choppy trades more aggressively
2. Stable volatility window (12 bars)
3. Adjusted stop/target bounds (0.65-1.5 / 0.85-2.5)
4. MA periods fast=5, slow=19
5. Bar direction confirmation filter

Key insight from #7's success:
The min_ma_divergence threshold is critical. Higher values filter out more choppy,
low-conviction trades. The jump from 0.001 to 0.0015 gave +0.4035 improvement.

Further optimization opportunities:
1. The min_ma_divergence of 0.0015 (0.15%) might still have room to increase.
   - Try 0.002 (0.2%) to be even more selective about trend strength
   - This would trade fewer times but with higher conviction
   
2. Volatility adjustment might be over-aggressive.
   - Current: vol_adjustment = 1.0 + (vol * 1.2)
   - Try reducing vol_multiplier to 1.0 for less aggressive scaling
   - This keeps stops tighter on high volatility days
   
3. Stop/target bounds could be optimized further:
   - Current min_stop=0.65 might be slightly wide
   - Try min_stop=0.6 (tighter, reduce false whipsaws)
   - Current max_stop=1.5 might be slightly tight
   - Try max_stop=1.7 (allow wider stops on high vol)
   
4. MA periods: fast=5, slow=19 has been consistent. But:
   - Try fast=4, slow=20 for slightly faster trend detection
   - OR fast=6, slow=18 for more conservative trend confirmation
   
5. The bar direction confirmation is powerful. Consider:
   - Also require the bar to close beyond its open by minimum amount
   - E.g., require close > open + 0.1 for buy (not just close > open)
   - This filters out barely-up bars
   
6. Entry price condition (current >= fast_ma):
   - Currently allows entry right at the MA
   - Try requiring price > fast_ma (stricter pullback requirement)
   - This might reduce early entries in reversals

This strategy attempts to improve on #7 by:
- Increasing min_ma_divergence from 0.0015 to 0.002 (even more selective)
- Reducing vol_multiplier from 1.2 to 1.0 (less aggressive vol scaling)
- Adjusting min_stop from 0.65 to 0.6 (tighter stops)
- Adjusting max_stop from 1.5 to 1.7 (allow wider stops on high vol)
- Adding bar strength requirement: bar must move by 0.1 in trend direction
- Keeping fast=5, slow=19 (the working periods)
- Keeping target adjustments: min_target=0.85, max_target=2.5
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 5,
                 slow_lookback: int = 19, vol_lookback: int = 12,
                 vol_multiplier: float = 1.0, min_stop: float = 0.6,
                 max_stop: float = 1.7, min_target: float = 0.85,
                 max_target: float = 2.5, min_ma_divergence: float = 0.002,
                 min_bar_strength: float = 0.1, size: int = 1):
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
        self.min_bar_strength = min_bar_strength
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
        
        # Trend-following with strong confirmation and divergence filter
        # Only trade if MAs are sufficiently diverged (not in choppy range)
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Calculate bar strength (how much it moved in its direction)
        bar_strength = abs(last_bar.close - last_bar.open)
        
        # Buy: uptrend (fast > slow) + price >= fast MA + bar closed up + strong bar
        if (fast_ma > slow_ma and current_price >= fast_ma and 
            last_bar.close > last_bar.open and bar_strength >= self.min_bar_strength):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + bar closed down + strong bar
        elif (fast_ma < slow_ma and current_price <= fast_ma and 
              last_bar.close < last_bar.open and bar_strength >= self.min_bar_strength):
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
