"""
Analysis of trial #22 (+1.2900, best so far):

Trial #22 achieved +1.2900 with refined parameters:
- vol_multiplier_stop: 1.13
- vol_multiplier_target: 1.46
- bar_strength_threshold: 0.38
- min_ma_divergence: 0.00144
- price_tolerance: 0.0012
- min_target: 0.95, max_target: 2.85
- min_stop: 0.70

The core insight is that the dual-MA (fast=6, slow=20) with asymmetric volatility
scaling creates a robust edge. The asymmetry (1.13 for stops vs 1.46 for targets)
favors larger profit targets relative to risk, which is mathematically sound.

For trial #23, I'll make targeted refinements to push beyond +1.29:

1. Optimize the MA divergence threshold: The current 0.00144 is quite tight. Testing
   a slightly looser threshold (0.00150) might capture more valid trades without
   sacrificing quality.

2. Refine the volatility multipliers further:
   - vol_multiplier_stop: 1.13 → 1.14 (slightly tighter stops, better risk control)
   - vol_multiplier_target: 1.46 → 1.48 (slightly larger targets, better reward)
   This increases the asymmetry which has proven effective.

3. Adjust bar_strength_threshold: Current 0.38 is moderate. Try 0.36 to be slightly
   more inclusive of valid directional bars without being too permissive.

4. Fine-tune price_tolerance: Current 0.0012 is tight. Try 0.0013 to give slightly
   more room around the fast MA to reduce whipsaws while staying responsive.

5. Expand the target range slightly: min_target 0.95 → 0.92 to capture smaller
   favorable moves, max_target 2.85 → 2.95 to allow larger targets in high vol.

The philosophy is that #22 found a near-optimal configuration, and these micro-
adjustments should push it incrementally higher by:
- Slightly more inclusive entry conditions (lower divergence threshold, lower strength)
- Better risk/reward asymmetry (wider target range, tighter stops)
- Reduced whipsaw (higher price tolerance)
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 13,
                 vol_multiplier_stop: float = 1.14,
                 vol_multiplier_target: float = 1.48,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.92, max_target: float = 2.95,
                 min_ma_divergence: float = 0.00150,
                 bar_strength_threshold: float = 0.36,
                 price_tolerance: float = 0.0013,
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
        self.price_tolerance = price_tolerance
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
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up + bar has strength
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and bar_strength >= self.bar_strength_threshold):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down + bar has strength
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
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
