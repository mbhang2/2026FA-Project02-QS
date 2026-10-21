"""
Analysis of trial #24 (+0.5850, significant regression from #22's +1.2900):

The momentum acceleration filter and volatility regime classification in #24 were
too restrictive, cutting trade count from 647 to 472 and degrading performance.

Looking at the trial history:
- #22: +1.2900 (647 trades) - dual MA with asymmetric volatility scaling
- #19: +1.2897 (647 trades) - nearly identical to #22
- #23: +1.1743 (649 trades) - over-optimized parameters
- #24: +0.5850 (472 trades) - too many filters, too few trades

The clear pattern: #22's configuration is near-optimal. The parameters that work:
- vol_multiplier_stop: 1.13
- vol_multiplier_target: 1.46
- bar_strength_threshold: 0.38
- min_ma_divergence: 0.00144
- price_tolerance: 0.0012
- min_target: 0.95, max_target: 2.85
- min_stop: 0.70

For trial #25, I'll take a completely different approach. Instead of tweaking the
dual-MA system further, I'll explore a multi-timeframe confirmation strategy:

1. Keep 5m as the primary decision timeframe
2. Add 1h as a trend confirmation (only trade with the hourly trend)
3. Use the 5m for entry timing, 1h for direction bias
4. This reduces false signals without cutting trade count as severely as #24

The hypothesis: many of the 647 trades in #22 are whipsaws that could be avoided
with higher-timeframe confirmation. By adding 1h confirmation, we filter the weakest
trades while keeping the bulk of the edge.

This is a fundamentally different approach than parameter tuning - it's a structural
change that uses multiple timeframes to improve signal quality.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, fast_interval: str = "5m", slow_interval: str = "1h",
                 fast_lookback: int = 6, slow_lookback: int = 20, vol_lookback: int = 13,
                 vol_multiplier_stop: float = 1.13,
                 vol_multiplier_target: float = 1.46,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.38,
                 price_tolerance: float = 0.0012,
                 size: int = 1):
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
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
        # Accumulate closes for MA and volatility calculations on fast timeframe
        self.fast_closes: list[float] = []
        # Track returns for volatility on fast timeframe
        self.returns: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # Only act when the fast bar closes
        if self.fast_interval not in ctx.bars:
            return

        fast_history = ctx.history(self.fast_interval)
        if len(fast_history) < 1:
            return

        last_fast_bar = fast_history[-1]
        current_price = last_fast_bar.close

        # Accumulate fast closes
        self.fast_closes.append(current_price)
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + 5
        if len(self.fast_closes) > max_needed:
            self.fast_closes = self.fast_closes[-max_needed:]

        # Calculate returns for volatility on fast timeframe
        if len(self.fast_closes) >= 2:
            prev_price = self.fast_closes[-2]
            if prev_price > 0:
                ret = (current_price - prev_price) / prev_price
                self.returns.append(ret)
                if len(self.returns) > self.vol_lookback + 5:
                    self.returns = self.returns[-(self.vol_lookback + 5):]

        # Need enough data for moving averages on fast timeframe
        if len(self.fast_closes) < self.slow_lookback:
            return

        # Calculate fast and slow moving averages on fast timeframe
        fast_closes_window = self.fast_closes[-self.fast_lookback:]
        slow_closes_window = self.fast_closes[-self.slow_lookback:]
        
        fast_ma = sum(fast_closes_window) / len(fast_closes_window)
        slow_ma = sum(slow_closes_window) / len(slow_closes_window)
        
        if slow_ma <= 0:
            return
        
        # Calculate volatility on fast timeframe
        vol = 0.0
        if len(self.returns) >= self.vol_lookback:
            recent_returns = self.returns[-self.vol_lookback:]
            mean_ret = sum(recent_returns) / len(recent_returns)
            variance = sum((r - mean_ret) ** 2 for r in recent_returns) / len(recent_returns)
            vol = math.sqrt(max(variance, 0.0))
        
        # Calculate MA divergence on fast timeframe (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if fast MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Get hourly trend confirmation
        slow_history = ctx.history(self.slow_interval)
        if len(slow_history) < 1:
            return
        
        last_slow_bar = slow_history[-1]
        
        # Determine hourly trend (simple: close vs open)
        hourly_uptrend = last_slow_bar.close > last_slow_bar.open
        hourly_downtrend = last_slow_bar.close < last_slow_bar.open
        
        # Calculate bar strength (directional conviction)
        bar_range = last_fast_bar.high - last_fast_bar.low
        if bar_range <= 0:
            return
        
        bar_body = abs(last_fast_bar.close - last_fast_bar.open)
        bar_strength = bar_body / bar_range
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend on fast (fast > slow) + hourly uptrend + price >= fast MA - tolerance 
        #      + bar closed up + bar has strength
        if (fast_ma > slow_ma and hourly_uptrend and 
            current_price >= (fast_ma - tolerance_band) and 
            last_fast_bar.close > last_fast_bar.open and 
            bar_strength >= self.bar_strength_threshold):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend on fast (fast < slow) + hourly downtrend + price <= fast MA + tolerance 
        #       + bar closed down + bar has strength
        elif (fast_ma < slow_ma and hourly_downtrend and 
              current_price <= (fast_ma + tolerance_band) and 
              last_fast_bar.close < last_fast_bar.open and 
              bar_strength >= self.bar_strength_threshold):
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
