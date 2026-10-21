"""
Analysis of trial progression and optimization strategy.

Trial scores progression:
- #0-#4: Negative to near-zero (baseline attempts)
- #5-#6: +0.3776 to +0.3976 (MA divergence + vol scaling)
- #7: +0.8011 (min_ma_divergence=0.0015 breakthrough)
- #8-#9: +0.7282 to +0.7305 (regressions from #7)
- #10: +0.9545 (slow_lookback=20, vol_lookback=13 improvements)
- #11: +0.9686 (fast_lookback=6, vol_multiplier=1.25)
- #12: +0.9941 (best so far - asymmetric vol scaling, bar_strength filter)

Trial #12 achieved +0.9941, nearly optimal performance. The key innovations:
1. Asymmetric volatility scaling (vol_multiplier_stop=1.2, vol_multiplier_target=1.35)
2. Bar strength filter (bar_strength_threshold=0.4) for directional conviction
3. Tight MA divergence threshold (0.0015) filters choppy trades
4. slow_lookback=20, vol_lookback=13 provide stable trend/volatility estimates

The score of +0.9941 is very close to 1.0, suggesting we're near the optimal
configuration. Further improvements should focus on:

1. Fine-tuning the bar strength threshold:
   - Current: 0.4 (bar body is 40% of range)
   - Try: 0.38 (slightly more permissive, catch slightly weaker bars)
   - This could catch more valid signals without introducing noise

2. Refining MA divergence threshold:
   - Current: 0.0015 (very tight, filters choppy markets)
   - Try: 0.00148 (very slight decrease to catch slightly weaker trends)
   - The difference is minimal but might capture additional valid setups

3. Volatility multiplier asymmetry optimization:
   - Current: stop=1.2, target=1.35 (ratio 1.125)
   - Try: stop=1.18, target=1.38 (ratio 1.169)
   - Increases target scaling slightly more relative to stop

4. Price confirmation refinement:
   - Current: price >= fast_ma for buy, price <= fast_ma for sell
   - Try: Add tolerance band to reduce whipsaws at MA crossover
   - Example: price >= (fast_ma - 0.001 * slow_ma) for buy
   - This prevents triggering on tiny overshoots

5. Maximum target ceiling:
   - Current: 2.7
   - Try: 2.75 (slightly higher to allow bigger wins on strong trends)
   - Combined with target multiplier increase

For trial #13, I'll implement subtle refinements to push past +0.9941:
- Decrease bar_strength_threshold to 0.38 (catch more valid signals)
- Decrease min_ma_divergence to 0.00148 (catch slightly weaker trends)
- Adjust vol multipliers to stop=1.18, target=1.38 (better asymmetry)
- Increase max_target to 2.75 (allow bigger wins)
- Add price tolerance band to reduce MA crossover whipsaws
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 13,
                 vol_multiplier_stop: float = 1.18,
                 vol_multiplier_target: float = 1.38,
                 min_stop: float = 0.68, max_stop: float = 1.5,
                 min_target: float = 0.9, max_target: float = 2.75,
                 min_ma_divergence: float = 0.00148,
                 bar_strength_threshold: float = 0.38,
                 price_tolerance: float = 0.001,
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
