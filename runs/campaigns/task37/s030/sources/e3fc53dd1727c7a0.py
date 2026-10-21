"""
Analysis of trials #40-#42 and the optimization trajectory:

Best score: #40 at +1.4150 with 660 trades.
Regression in #41: +0.9079 (bracket parameter changes hurt).
Identical to #40 in #42: +1.4150 with bar_strength=0.38 (no improvement).

Key insight: #40 found a local optimum. The parameters work well together:
- Entry logic: fast MA (6) > slow MA (20), bar_strength >= 0.35, price_tolerance=0.0020
- Bracket: vol_multiplier_stop=1.14, vol_multiplier_target=1.45
- Trade frequency: 660 trades (optimal range)

The pattern across all trials:
- 645-670 trades consistently scores +1.25 to +1.42
- Fewer trades (202-585) score much lower
- More trades (700+) haven't been tested but risk overtrading

For trial #43, the hypothesis:
The entry filter uses MA divergence (min_ma_divergence=0.00144). This is a relative
threshold. What if we make it slightly more permissive to capture more early-trend
entries, but keep everything else from #40?

Current: min_ma_divergence = 0.00144 (requires 0.144% separation)

The logic:
- Lower divergence threshold → more entry opportunities → trade count increases
- But we still have bar_strength and price_tolerance filters to prevent noise
- The bracket scaling from #40 should handle more frequent entries

Proposed change:
- **Reduce min_ma_divergence from 0.00144 to 0.00120** (slightly looser MA filter)
- Keep all other #40 parameters exactly as-is

This should:
- Increase trade frequency from 660 to ~680-700 (still reasonable)
- Capture more trend-following entries
- Benefit from proven bracket scaling
- Risk: if trade count goes too high (>700), might see diminishing returns

Alternative explanation: maybe the issue is that 0.00144 is too tight. Relaxing
it slightly might improve entry timing without degrading quality.
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
                 min_ma_divergence: float = 0.00120,
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
