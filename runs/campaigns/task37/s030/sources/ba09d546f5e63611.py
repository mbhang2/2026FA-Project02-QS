"""
Analysis of trials #40-#46 and strategic direction:

Best score: #40 at +1.4150 with 660 trades.
All subsequent attempts have failed to improve or regressed.

Key observations from the plateau:
1. #40's parameters are well-tuned: fast=6, slow=20, bar_strength=0.35
2. Volatility-scaled brackets (vol_multiplier_stop=1.14, target=1.45) work
3. Trade counts of 645-670 consistently score +1.25 to +1.42
4. #44-#45 showed that removing volatility scaling hurts significantly
5. #46 tried loosening thresholds but scored +1.2633 (improvement but not enough)

The pattern suggests we're stuck in a local optimum. The issue might be:
- The entry logic (MA crossover + bar strength) is sound but perhaps too rigid
- We're not exploring enough of the parameter space systematically
- The volatility calculation might be the bottleneck

For trial #47, new hypothesis:
What if the issue is that we're calculating volatility from *returns* (percentage
changes) but the synthetic data's price movements might not follow realistic
distributions? What if we use a simpler, more direct volatility measure based on
recent bar ranges instead?

Also, #40 had vol_multiplier_stop=1.14 (conservative) and target=1.45
(aggressive). What if we increase stop slightly to 1.20 and keep target at 1.45?
This would make stops tighter (better risk control) while keeping targets the
same, which might improve the risk-reward ratio.

Additionally, let's try slightly different MA lookbacks:
- fast_lookback: 6 (proven)
- slow_lookback: 22 (instead of 20, slightly longer)
- This might reduce false crossovers

And adjust the bar_strength_threshold to 0.36 (slightly stricter than #46's 0.32,
back toward #40's 0.35 but slightly looser).

This is a careful refinement around #40's proven parameters, with a focus on:
1. Volatility calculation method (bar range instead of returns)
2. Slightly tighter stops (vol_multiplier_stop=1.20)
3. Slightly longer slow MA (22 instead of 20)
4. Balanced bar strength threshold (0.36)
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 22, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.20,
                 vol_multiplier_target: float = 1.45,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.36,
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
        # Accumulate bar ranges for volatility
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
        
        # Accumulate bar ranges
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
        
        # Calculate volatility using average bar range (more direct measure)
        vol = 0.0
        if len(self.bar_ranges) >= self.vol_lookback:
            recent_ranges = self.bar_ranges[-self.vol_lookback:]
            avg_range = sum(recent_ranges) / len(recent_ranges)
            # Normalize by current price to get volatility as a fraction
            if current_price > 0:
                vol = avg_range / current_price
        
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
