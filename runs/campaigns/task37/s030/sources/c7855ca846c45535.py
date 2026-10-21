"""
Analysis of trial #23 (+1.1743, regression from #22's +1.2900):

Trial #23's micro-adjustments actually degraded performance, suggesting we 
over-optimized. The parameters that worked best were in #22:
- vol_multiplier_stop: 1.13
- vol_multiplier_target: 1.46
- bar_strength_threshold: 0.38
- min_ma_divergence: 0.00144
- price_tolerance: 0.0012

The regression from +1.29 to +1.17 in #23 tells us the direction of change was wrong.
Looking at the trial history, #22 (+1.2900) and #19 (+1.2897) are nearly identical
in score, both using similar dual-MA with asymmetric volatility scaling.

For trial #24, I'll take a different approach: instead of micro-tuning parameters
around the dual-MA approach, I'll explore a more sophisticated entry signal that
combines:

1. The proven dual-MA framework (fast=6, slow=20) as the trend filter
2. Add momentum confirmation via price acceleration (rate of change of the MA divergence)
3. Add a secondary volatility regime filter that trades more aggressively in low vol
4. Tighter entry conditions but with better risk/reward through dynamic sizing

The key insight: #22 was already near-optimal. Rather than tweak individual parameters,
I should add a layer of sophistication that filters entries better without reducing
trade frequency too much. The momentum acceleration filter will catch the strongest
trends while the volatility regime adjustment will scale risk appropriately.

Changes for trial #24:
1. Keep core #22 parameters as baseline
2. Add momentum acceleration: track rate of change of MA divergence
3. Add volatility regime classification (low/medium/high vol)
4. Scale stop/target based on volatility regime, not just raw volatility
5. Require momentum to be accelerating (divergence growing) for entries
6. Keep trade count high by being more selective on quality, not quantity
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 13,
                 momentum_lookback: int = 3,
                 vol_multiplier_stop: float = 1.13,
                 vol_multiplier_target: float = 1.46,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.38,
                 price_tolerance: float = 0.0012,
                 momentum_threshold: float = 0.000005,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.momentum_lookback = momentum_lookback
        self.vol_multiplier_stop = vol_multiplier_stop
        self.vol_multiplier_target = vol_multiplier_target
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
        self.bar_strength_threshold = bar_strength_threshold
        self.price_tolerance = price_tolerance
        self.momentum_threshold = momentum_threshold
        self.size = size
        # Accumulate closes for MA and volatility calculations
        self.closes: list[float] = []
        # Track returns for volatility
        self.returns: list[float] = []
        # Track MA divergences for momentum
        self.divergences: list[float] = []

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
        max_needed = self.slow_lookback + self.vol_lookback + self.momentum_lookback + 10
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]

        # Calculate returns for volatility
        if len(self.closes) >= 2:
            prev_price = self.closes[-2]
            if prev_price > 0:
                ret = (current_price - prev_price) / prev_price
                self.returns.append(ret)
                if len(self.returns) > self.vol_lookback + 10:
                    self.returns = self.returns[-(self.vol_lookback + 10):]

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
        
        # Track divergence for momentum
        self.divergences.append(ma_divergence)
        if len(self.divergences) > self.momentum_lookback + 5:
            self.divergences = self.divergences[-(self.momentum_lookback + 5):]
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Check momentum: divergence should be growing (accelerating)
        if len(self.divergences) >= self.momentum_lookback:
            recent_divs = self.divergences[-self.momentum_lookback:]
            # Calculate trend of divergence (simple linear momentum)
            momentum = (recent_divs[-1] - recent_divs[0]) / self.momentum_lookback
            if momentum < self.momentum_threshold:
                return  # Divergence not accelerating enough
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        # Volatility regime classification
        vol_percentile = self._estimate_vol_percentile(vol)
        
        # Scale brackets based on volatility regime
        # Low vol: tighter stops, larger targets (capture small moves)
        # High vol: wider stops, smaller targets (respect volatility)
        if vol_percentile < 0.33:
            regime_stop_mult = 0.9
            regime_target_mult = 1.1
        elif vol_percentile < 0.67:
            regime_stop_mult = 1.0
            regime_target_mult = 1.0
        else:
            regime_stop_mult = 1.1
            regime_target_mult = 0.9
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, 
                         self.min_stop * vol_adjustment_stop * regime_stop_mult))
        target_offset = max(self.min_target, min(self.max_target, 
                           self.min_target * vol_adjustment_target * regime_target_mult))
        
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

    def _estimate_vol_percentile(self, vol: float) -> float:
        """Estimate volatility percentile based on historical values."""
        if len(self.returns) < self.vol_lookback:
            return 0.5
        
        # Calculate volatility for multiple recent windows
        vols = []
        for i in range(max(1, len(self.returns) - self.vol_lookback * 3), len(self.returns) - self.vol_lookback + 1):
            window = self.returns[i:i + self.vol_lookback]
            if len(window) == self.vol_lookback:
                mean_ret = sum(window) / len(window)
                variance = sum((r - mean_ret) ** 2 for r in window) / len(window)
                vols.append(math.sqrt(max(variance, 0.0)))
        
        if not vols:
            return 0.5
        
        # Count how many historical vols are below current vol
        below = sum(1 for v in vols if v < vol)
        return below / len(vols) if vols else 0.5

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
