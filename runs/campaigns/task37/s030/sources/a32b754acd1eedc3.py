"""
Analysis of trial progression and optimization strategy.

Trial scores progression:
- #0-#4: Negative to near-zero (baseline attempts)
- #5-#6: +0.3776 to +0.3976 (MA divergence + vol scaling)
- #7: +0.8011 (min_ma_divergence=0.0015 breakthrough)
- #8-#9: +0.7282 to +0.7305 (regressions from #7)
- #10: +0.9545 (slow_lookback=20, vol_lookback=13 improvements)
- #11: +0.9686 (best so far - fast_lookback=6, vol_multiplier=1.25, min_stop=0.68, max_target=2.6)

Trial #11 achieved +0.9686, a new peak. The incremental improvements suggest:
1. fast_lookback=6 provides better balance than 5
2. vol_multiplier=1.25 scales position sizing optimally
3. min_stop=0.68 reduces whipsaws effectively
4. max_target=2.6 allows for better win capture
5. slow_lookback=20 remains optimal for trend identification
6. vol_lookback=13 is stable for volatility estimation

The pattern shows diminishing returns from parameter tweaking. Next optimization
should explore:

1. Entry signal refinement: Add a secondary confirmation filter
   - Current: fast > slow AND price >= fast_ma AND bar up
   - Try: Add bar close proximity to fast MA as confirmation
   - This could filter out marginal entries that reverse quickly

2. Exit optimization: Asymmetric risk/reward based on trend strength
   - Current: vol_adjustment applied uniformly to both stop and target
   - Try: Scale target more aggressively when MA divergence is very high
   - This captures bigger moves when trends are strong

3. MA divergence threshold fine-tuning: Current 0.0015 is proven good
   - Try: 0.00148 (very slight decrease) to catch slightly weaker trends
   - Or keep at 0.0015 if the signal is already optimal

4. Volatility scaling asymmetry:
   - Current: vol_adjustment = 1.0 + (vol * vol_multiplier)
   - Try: Different multipliers for stop vs target
   - Example: vol_multiplier_stop=1.2, vol_multiplier_target=1.3
   - This could improve risk/reward ratio on volatile moves

5. Bar confirmation: Instead of just bar.close > bar.open
   - Try: Check bar.close > bar.open AND (bar.close - bar.open) / bar.range > threshold
   - This ensures the bar has meaningful directional conviction

For trial #12, I'll implement asymmetric volatility scaling and enhanced bar
confirmation to push beyond +0.9686. The key insight is that strong trends
(high MA divergence) should get wider targets relative to stops, and bars
with strong directional conviction should be preferred.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 13,
                 vol_multiplier_stop: float = 1.2,
                 vol_multiplier_target: float = 1.35,
                 min_stop: float = 0.68, max_stop: float = 1.5,
                 min_target: float = 0.9, max_target: float = 2.7,
                 min_ma_divergence: float = 0.0015,
                 bar_strength_threshold: float = 0.4,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.vol_multiplier_stop = vol_multiplier_stop
        self.vol_multiplier_target = vol_multiplier_target
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
        self.bar_strength_threshold = bar_strength_threshold
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
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Buy: uptrend (fast > slow) + price >= fast MA + bar closed up + bar has strength
        if (fast_ma > slow_ma and current_price >= fast_ma and 
            last_bar.close > last_bar.open and bar_strength >= self.bar_strength_threshold):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + bar closed down + bar has strength
        elif (fast_ma < slow_ma and current_price <= fast_ma and 
              last_bar.close < last_bar.open and bar_strength >= self.bar_strength_threshold):
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
