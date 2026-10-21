"""
Analysis of trials #30-#37 and strategic direction:

The best score is #30 at +1.3269 with 653 trades. Despite 7 subsequent attempts,
none have improved. The plateau suggests we've optimized the current approach to
its limit.

Key observations:
1. Trade count of 645-653 is optimal; deviations hurt
2. The core (fast=6, slow=20, bar_strength=0.39) is robust
3. Bar strength-responsive brackets (#37) scored +1.2715 - regression
4. Adaptive tolerance (#36) scored +1.2734 - regression
5. Every modification to exit logic has underperformed

Critical insight: all recent failures attempted to *improve exits* while keeping
entries constant. What if the issue is that we're entering on *too many* bars?

The current entry logic triggers whenever:
- Fast MA > Slow MA (uptrend)
- Price within tolerance of fast MA
- Bar strength > 0.39
- Bar closed up

This generates ~645-653 trades. But what if many of these entries are in
marginal setups that the bracket exits quickly for small losses?

New hypothesis for trial #38:
Instead of modifying bracket sizing (which hasn't worked), make entry selection
more selective by requiring *sustained* trend confirmation. Add a requirement
that the fast MA has been above slow MA for at least N bars, not just at this
instant. This filters out whipsaw entries at trend reversals.

Implementation:
1. Keep proven core parameters (fast=6, slow=20, bar_strength=0.39, tolerance=0.0015)
2. Keep proven bracket scaling (vol_multiplier_stop=1.14, vol_multiplier_target=1.45)
3. Add trend_confirmation_bars=2: only enter if the trend direction has held for 2+ bars
4. This should reduce trade count slightly (to ~600-630) but improve quality
5. The fewer but higher-conviction entries should score better

This targets entry *quality* rather than exit mechanics, which is where all
recent improvements failed.
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
                 trend_confirmation_bars: int = 2,
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
        self.trend_confirmation_bars = trend_confirmation_bars
        self.size = size
        # Accumulate closes for MA and volatility calculations
        self.closes: list[float] = []
        # Track returns for volatility
        self.returns: list[float] = []
        # Track trend direction history for confirmation
        self.trend_direction: list[int] = []  # 1 for up, -1 for down, 0 for neutral

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
        
        # Determine current trend direction
        if fast_ma > slow_ma:
            current_trend = 1  # uptrend
        elif fast_ma < slow_ma:
            current_trend = -1  # downtrend
        else:
            current_trend = 0  # neutral
        
        # Track trend direction
        self.trend_direction.append(current_trend)
        if len(self.trend_direction) > self.trend_confirmation_bars + 1:
            self.trend_direction = self.trend_direction[-(self.trend_confirmation_bars + 1):]
        
        # Check if trend has been confirmed for enough bars
        if len(self.trend_direction) < self.trend_confirmation_bars + 1:
            return
        
        # Verify that the trend has held consistently
        recent_trend = self.trend_direction[-self.trend_confirmation_bars:]
        if not all(t == current_trend for t in recent_trend):
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up + trend confirmed
        if (current_trend == 1 and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down + trend confirmed
        elif (current_trend == -1 and current_price <= (fast_ma + tolerance_band) and 
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
