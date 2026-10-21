"""
Analysis of trials #40-#43 and the optimization trajectory:

Best score: #40 at +1.4150 with 660 trades.
Recent attempts (#41-#43) have failed to improve, all scoring <= +1.4080.

Key observations:
1. #40 remains the best with 660 trades and +1.4150
2. #41 (bracket changes): +0.9079 regression (wider stops hurt)
3. #42 (bar_strength=0.38): +1.4150 identical to #40 (no improvement)
4. #43 (looser MA filter): +1.4080 slight regression (too many trades?)

The pattern is clear: #40 found a local optimum. The current approach of tweaking
individual parameters has plateaued.

New hypothesis for trial #44:
The issue might be that we're optimizing for trade count, but the real constraint
is *entry timing* within the trend. Currently we use:
- fast MA (6 bars) > slow MA (20 bars)
- bar_strength >= 0.35
- price_tolerance = 0.0020

What if the problem is that we're entering too late in the trend? The fast MA at
6 bars is quite short, and by the time it crosses above the slow MA at 20 bars,
we might be entering mid-trend rather than early-trend.

Alternative hypothesis: volatility scaling might be the issue. In #40, we use:
- vol_multiplier_stop = 1.14
- vol_multiplier_target = 1.45

But what if the volatility calculation itself needs refinement? Currently we use
standard deviation of returns, which is symmetric. For a trend-following strategy,
we might want to emphasize *directional volatility* (how much the price moves in
the trend direction) rather than absolute volatility.

For trial #44, let's try a hybrid approach:
1. Keep the proven entry logic from #40 (fast=6, slow=20, bar_strength=0.35)
2. Keep the proven price_tolerance=0.0020
3. **Modify the volatility calculation to use recent bar ranges instead of returns**
   - This captures directional movement better for trend-following
   - Should improve bracket scaling for the actual market movement
4. Keep min_ma_divergence=0.00144 (proven threshold)
5. Slightly adjust bracket multipliers based on the new volatility measure

This targets the gap: maybe the issue isn't entry frequency but bracket sizing
relative to actual market volatility. Using bar ranges (high-low) instead of
returns might better reflect the volatility that our positions actually experience.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.18,
                 vol_multiplier_target: float = 1.50,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.35,
                 price_tolerance: float = 0.0020,
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
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Track bar ranges for volatility
        self.bar_ranges: list[float] = []

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
        
        # Accumulate bar ranges (high - low)
        bar_range = last_bar.high - last_bar.low
        self.bar_ranges.append(bar_range)
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + 5
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.bar_ranges) > max_needed:
            self.bar_ranges = self.bar_ranges[-max_needed:]

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
        
        # Calculate volatility using bar ranges (directional volatility)
        vol = 0.0
        if len(self.bar_ranges) >= self.vol_lookback:
            recent_ranges = self.bar_ranges[-self.vol_lookback:]
            mean_range = sum(recent_ranges) / len(recent_ranges)
            if mean_range > 0:
                # Normalize range volatility
                variance = sum((r - mean_range) ** 2 for r in recent_ranges) / len(recent_ranges)
                vol = math.sqrt(max(variance, 0.0)) / mean_range
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Calculate bar strength (directional conviction)
        if bar_range <= 0:
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        if bar_strength < self.bar_strength_threshold:
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open):
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
