"""
Analysis of trials #30-#36 and strategic pivot:

The best score remains #30 at +1.3269 with 653 trades. All subsequent attempts
have either plateaued or regressed, suggesting we've hit the ceiling of the
current approach.

Key insights from the trial history:
1. Trade counts of 645-653 are optimal; deviations hurt significantly
2. The core formula (fast=6, slow=20, bar_strength=0.39) is robust
3. Adaptive tolerance (#36) scored +1.2734 - slight regression from #30
4. Scale-in strategies (#35) regressed badly despite higher trade counts
5. Adding complexity consistently underperforms the simple baseline

The pattern suggests the issue is not entry logic but *exit efficiency*. Current
brackets use fixed offsets scaled only by volatility. What if we're leaving money
on the table by exiting too early in strong trends, or holding too long in weak ones?

For trial #37, I'll test: what if we make bracket sizing more responsive to
*realized* bar momentum, not just historical volatility?

New hypothesis:
1. Keep proven entry core (fast=6, slow=20, bar_strength=0.39, tolerance=0.0015)
2. Make stop/target offsets respond to the *strength of the entry bar itself*
3. Strong bars (high bar_strength) get wider targets, tighter stops (reduce risk)
4. Weak bars get narrower targets, wider stops (protect against whipsaw)
5. This should improve exit quality without changing entry frequency

The key insight: the entry bar's strength predicts momentum continuation.
A 0.8 bar_strength entry likely has more fuel than a 0.39 entry, so it
deserves a different bracket structure.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 base_stop: float = 0.70, base_target: float = 0.95,
                 vol_multiplier_stop: float = 1.14,
                 vol_multiplier_target: float = 1.45,
                 min_stop: float = 0.50, max_stop: float = 1.8,
                 min_target: float = 0.70, max_target: float = 3.5,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.39,
                 price_tolerance: float = 0.0015,
                 bar_strength_multiplier: float = 0.8,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.base_stop = base_stop
        self.base_target = base_target
        self.vol_multiplier_stop = vol_multiplier_stop
        self.vol_multiplier_target = vol_multiplier_target
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
        self.bar_strength_threshold = bar_strength_threshold
        self.price_tolerance = price_tolerance
        self.bar_strength_multiplier = bar_strength_multiplier
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
        
        if bar_strength < self.bar_strength_threshold:
            return
        
        # Base volatility scaling
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        # Adjust brackets based on bar strength
        # Strong bars (high bar_strength) get wider targets, tighter stops
        # Weak bars get narrower targets, wider stops
        strength_factor = 1.0 + (bar_strength - self.bar_strength_threshold) * self.bar_strength_multiplier
        
        stop_offset = max(
            self.min_stop,
            min(self.max_stop, self.base_stop * vol_adjustment_stop / strength_factor)
        )
        target_offset = max(
            self.min_target,
            min(self.max_target, self.base_target * vol_adjustment_target * strength_factor)
        )
        
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
