"""
Analysis of trials #30-#38 and the path forward:

The best score remains #30 at +1.3269 with 653 trades. All 8 subsequent attempts
have failed to improve, with most regressing significantly. This plateau is
diagnostic: we've optimized the current approach to exhaustion.

Key pattern in the data:
- Trials modifying entry logic (trend_confirmation, bar_strength_responsive):
  regressed (#37: +1.2715, #38: +0.9367)
- Trials modifying exit logic (adaptive tolerance, strength-based brackets):
  regressed (#36: +1.2734, #37: +1.2715)
- The baseline formula (fast=6, slow=20, bar_strength=0.39) is robust
- Trade count of 645-653 is optimal

Critical insight: The issue may not be *what* we're trading, but *how much*.
Current approach trades every setup that passes the filters. But what if the
synthetic data has a specific optimal trading frequency that we're overshooting?

Trial #38 reduced trades via trend confirmation and scored +0.9367 - a major
regression. This suggests the problem isn't too many trades, it's something else.

New hypothesis for trial #39:
What if the issue is *timing precision*? The current strategy enters at bar
close, but the bracket is armed at fill time. There's a gap where we're
vulnerable. What if we:

1. Keep the proven entry core (fast=6, slow=20, bar_strength=0.39, tolerance=0.0015)
2. Keep the proven bracket scaling (vol_multiplier_stop=1.14, vol_multiplier_target=1.45)
3. But add a *secondary filter* based on recent momentum/acceleration

Instead of just checking "did the bar close up?", check "is the momentum
accelerating?" by looking at bar closes over the last 3 bars. This filters for
entries with confirmed directional momentum, not just single-bar reversals.

This keeps trade frequency high (should still be 630-650) but improves entry
conviction by requiring momentum confirmation beyond a single bar.
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
                 bar_strength_threshold: float = 0.39,
                 price_tolerance: float = 0.0015,
                 momentum_lookback: int = 3,
                 min_momentum_ratio: float = 0.5,
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
        self.momentum_lookback = momentum_lookback
        self.min_momentum_ratio = min_momentum_ratio
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
        max_needed = self.slow_lookback + self.vol_lookback + self.momentum_lookback + 5
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
        
        if bar_strength < self.bar_strength_threshold:
            return
        
        # Check momentum: are recent bars confirming the direction?
        if len(self.closes) < self.momentum_lookback + 1:
            return
        
        momentum_closes = self.closes[-self.momentum_lookback:]
        
        # Calculate momentum ratio: how much of recent movement is in the current direction?
        if last_bar.close > last_bar.open:  # Potential long entry
            # For uptrend, check if recent bars are mostly up
            up_bars = sum(1 for i in range(len(momentum_closes) - 1) 
                         if momentum_closes[i + 1] > momentum_closes[i])
            momentum_ratio = up_bars / (len(momentum_closes) - 1)
        else:  # Potential short entry
            # For downtrend, check if recent bars are mostly down
            down_bars = sum(1 for i in range(len(momentum_closes) - 1) 
                           if momentum_closes[i + 1] < momentum_closes[i])
            momentum_ratio = down_bars / (len(momentum_closes) - 1)
        
        # Only trade if momentum confirms the direction
        if momentum_ratio < self.min_momentum_ratio:
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up + momentum confirmed
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down + momentum confirmed
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
