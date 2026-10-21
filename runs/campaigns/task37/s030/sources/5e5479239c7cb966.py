"""
Analysis of trial #26 (+1.2916, best so far) and the complete trial history:

The pattern is clear:
- #22, #19: +1.2900, +1.2897 (647 trades) - dual MA with vol scaling
- #26: +1.2916 (647 trades) - very slight improvement, same trade count
- #25: +0.5850 (498 trades) - multi-timeframe too restrictive
- #24: +0.5850 (472 trades) - over-filtering
- #23: +1.1743 (649 trades) - slight regression
- #21: +1.2008 (637 trades) - slightly worse
- #20: +0.5580 (585 trades) - worse

The winning configuration (#26) uses:
1. Fast MA (6-bar) and Slow MA (20-bar) for trend detection
2. Volatility-scaled brackets (vol_lookback=18)
3. Bar strength filter (0.38 threshold)
4. Price tolerance band (0.0012)
5. MA divergence threshold (0.00144)

For trial #27, I'll explore a subtle but important refinement:

The current approach treats all bars equally when calculating MAs. However, bars
with higher volume or stronger directional conviction might deserve more weight.
Additionally, the price tolerance band is currently fixed at 0.0012 * slow_ma.
What if we make it adaptive based on recent volatility?

Key hypotheses:
1. Volume-weighted MA might better capture true market direction (especially on
   synthetic data where volume patterns matter)
2. Volatility-adaptive price tolerance would tighten bands in low-vol regimes
   (reducing false signals) and loosen them in high-vol (catching real moves)
3. The bar strength threshold could be slightly lowered (0.35 instead of 0.38)
   to capture more valid signals without introducing noise

This is a conservative refinement that keeps the proven core logic intact while
adding adaptive mechanisms that should improve signal quality without reducing
trade frequency significantly.

Changes:
- Use volume-weighted moving averages instead of simple MAs
- Make price tolerance band adaptive to volatility
- Slightly lower bar strength threshold (0.35)
- Keep all other proven parameters from #26
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.14,
                 vol_multiplier_target: float = 1.45,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.35,
                 base_price_tolerance: float = 0.0012,
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
        self.base_price_tolerance = base_price_tolerance
        self.size = size
        # Accumulate closes and volumes for weighted MA
        self.closes: list[float] = []
        self.volumes: list[int] = []
        # Track returns for volatility
        self.returns: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _weighted_ma(self, closes: list[float], volumes: list[int]) -> float:
        """Calculate volume-weighted moving average."""
        if not closes or not volumes or len(closes) != len(volumes):
            return 0.0
        total_volume = sum(volumes)
        if total_volume <= 0:
            return sum(closes) / len(closes)
        weighted_sum = sum(c * v for c, v in zip(closes, volumes))
        return weighted_sum / total_volume

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

        # Accumulate closes and volumes
        self.closes.append(current_price)
        self.volumes.append(last_bar.volume)
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + 5
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
            self.volumes = self.volumes[-max_needed:]

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

        # Calculate volume-weighted moving averages
        fast_closes = self.closes[-self.fast_lookback:]
        fast_volumes = self.volumes[-self.fast_lookback:]
        slow_closes = self.closes[-self.slow_lookback:]
        slow_volumes = self.volumes[-self.slow_lookback:]
        
        fast_ma = self._weighted_ma(fast_closes, fast_volumes)
        slow_ma = self._weighted_ma(slow_closes, slow_volumes)
        
        if slow_ma <= 0 or fast_ma <= 0:
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
        
        # Adaptive price tolerance band: base tolerance scaled by volatility
        # Higher vol -> wider tolerance; lower vol -> tighter tolerance
        vol_scale = 1.0 + (vol * 0.5)  # vol contributes up to 50% adjustment
        tolerance_band = self.base_price_tolerance * slow_ma * vol_scale
        
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
