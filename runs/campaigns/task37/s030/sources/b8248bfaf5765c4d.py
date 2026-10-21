"""
Analysis of trials #40-#44 and strategic direction:

Best score: #40 at +1.4150 with 660 trades.
Recent attempts have plateaued or regressed:
- #41: +0.9079 (bracket changes hurt)
- #42: +1.4150 identical to #40 (no improvement)
- #43: +1.4080 (looser MA filter slightly worse)
- #44: +0.7190 (bar range volatility calculation failed)

The core issue: we've been micro-optimizing parameters around #40, but hitting
diminishing returns. The problem with #44 was likely the volatility calculation
change - switching from returns to bar ranges fundamentally altered the bracket
scaling behavior.

Key insight from the data:
- Trade counts of 645-670 consistently score +1.25 to +1.42
- #40's parameters are well-balanced: fast=6, slow=20, bar_strength=0.35
- The bracket scaling (vol_multiplier_stop=1.14, target=1.45) works well
- min_ma_divergence=0.00144 is the right threshold

For trial #45, hypothesis:
The issue might be that we're using volatility scaling at all. What if the
synthetic data doesn't reward volatility-adjusted brackets? What if fixed,
well-tuned bracket offsets work better than adaptive ones?

New approach:
1. Keep the proven entry logic: fast MA (6), slow MA (20), bar_strength (0.35)
2. Keep price_tolerance=0.0020 and min_ma_divergence=0.00144
3. **Remove volatility scaling entirely**
4. **Use fixed, optimized stop and target offsets** instead
5. Keep the simple return-based volatility calculation for reference, but don't use it

The rationale:
- #40 had vol_multiplier_stop=1.14 and target=1.45, with min_stop=0.70 and min_target=0.95
- This means minimum brackets were 0.70 and 0.95 (when vol=0)
- What if we just use fixed offsets: stop_offset=0.75, target_offset=1.10?
- These are in the middle of the #40 range and should work well
- Simpler logic = fewer opportunities for calculation errors
- Synthetic data might not have realistic volatility patterns anyway

This is a regression to simplicity, which often works when optimization has plateaued.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 stop_offset: float = 0.75, target_offset: float = 1.10,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.35,
                 price_tolerance: float = 0.0020,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.min_ma_divergence = min_ma_divergence
        self.bar_strength_threshold = bar_strength_threshold
        self.price_tolerance = price_tolerance
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Track returns for reference (not used in decision logic)
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

        # Calculate returns for reference/monitoring
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
        
        if bar_strength < self.bar_strength_threshold:
            return
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open):
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="trend-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
