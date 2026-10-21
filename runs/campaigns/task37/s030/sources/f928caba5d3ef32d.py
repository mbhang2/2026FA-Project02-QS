"""
Analysis of trial #28 (+0.6024, regression from #26's +1.2916):

The one-bar confirmation in #28 reduced trade frequency from 647 to 299, which
significantly hurt performance. This confirms that the simple, immediate entry
logic of #26 is superior.

Looking at the complete high-performing trials:
- #26: +1.2916 (647 trades) - simple MAs, fixed vol scaling, fixed tolerance
- #22: +1.2900 (647 trades) - nearly identical
- #19: +1.2897 (647 trades) - same core approach
- #27: +1.2287 (650 trades) - volume-weighted MAs (slight regression)
- #28: +0.6024 (299 trades) - one-bar confirmation (major regression)

The pattern is clear: the simple approach with immediate entry is best. The core
winning formula from #26 is:
1. Simple (non-weighted) fast MA (6) and slow MA (20)
2. Volatility-scaled brackets (vol_lookback=18, stop_mult=1.14, target_mult=1.45)
3. Bar strength filter (0.38 threshold)
4. Fixed price tolerance band (0.0012)
5. MA divergence threshold (0.00144)
6. Immediate entry (no confirmation delay)

For trial #29, I'll make a very conservative refinement to #26:

The hypothesis is that the current approach might benefit from slightly tighter
MA divergence detection. Instead of requiring abs(fast-slow)/slow >= 0.00144, we
could use 0.00142 to catch slightly earlier entries. Additionally, we can refine
the volatility cap logic slightly.

However, the safest approach is to test a small parameter sweep around #26's
proven values:
- min_ma_divergence: 0.00144 → 0.00148 (slightly stricter, higher conviction)
- vol_lookback: 18 → 19 (marginally smoother vol)
- bar_strength_threshold: 0.38 → 0.37 (slightly more permissive)

This maintains the winning core logic while testing if slightly different
parameter values can push past +1.2916.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 19,
                 vol_multiplier_stop: float = 1.14,
                 vol_multiplier_target: float = 1.45,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00148,
                 bar_strength_threshold: float = 0.37,
                 price_tolerance: float = 0.0012,
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
