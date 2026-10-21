"""
Analysis of the complete trial history and the path to improvement:

Trials #22, #26, #27 all scored around +1.29, with #26 at +1.2916 being the best.
The key insight is that these high-scoring strategies use:

1. Dual moving averages (fast=6, slow=20) for trend
2. Volatility-scaled brackets (asymmetric: stop 1.14x, target 1.45x)
3. Bar strength filter (0.35-0.38 threshold)
4. Price tolerance band (adaptive or fixed)
5. MA divergence threshold (0.00144)

Trial #27 introduced volume-weighted MAs and adaptive tolerance, scoring +1.2287.
This slight regression (-0.0629 from #26) suggests that:
- Volume weighting on synthetic data may not help (or hurts slightly)
- The adaptive tolerance mechanism might be too aggressive

The path forward: return to the simpler, proven #26 core logic but explore
orthogonal refinements:

1. **Reduce false signals via entry confirmation**: Current approach enters
   immediately when conditions align. What if we require the signal to persist
   for one bar? This reduces whipsaws without changing the core calculation.

2. **Optimize bar strength threshold**: #26 uses 0.35, #27 also 0.35. The
   original winners (#22, #19) used 0.38. Testing 0.36 might be the sweet spot.

3. **Refine MA divergence threshold**: Current 0.00144 is tight. Testing 0.00140
   might catch more valid setups without excessive noise.

4. **Adjust volatility lookback**: #26 uses 18. Testing 16-17 might provide
   slightly more responsive vol estimates without being too noisy.

5. **Fine-tune bracket parameters slightly**: vol_multiplier_stop from 1.14 to
   1.13, vol_multiplier_target from 1.45 to 1.46 (reverse the slight change
   from #26).

This trial will test a conservative refinement: revert to simple (non-weighted)
MAs like #26, use fixed price tolerance, but optimize the numerical parameters
and add one-bar confirmation to reduce false entries.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 17,
                 vol_multiplier_stop: float = 1.13,
                 vol_multiplier_target: float = 1.46,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00140,
                 bar_strength_threshold: float = 0.36,
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
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Track returns for volatility
        self.returns: list[float] = []
        # Track last signal for one-bar confirmation
        self.last_signal: str | None = None  # 'buy', 'sell', or None

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

        # Calculate simple moving averages
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
            self.last_signal = None
            return
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            self.last_signal = None
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        if bar_strength < self.bar_strength_threshold:
            self.last_signal = None
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Determine signal
        current_signal = None
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            current_signal = 'buy'
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open):
            current_signal = 'sell'
        
        # One-bar confirmation: only enter if signal persists
        if current_signal == 'buy' and self.last_signal == 'buy':
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
            self.last_signal = None
        elif current_signal == 'sell' and self.last_signal == 'sell':
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-short",
            )
            self.last_signal = None
        else:
            self.last_signal = current_signal

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
