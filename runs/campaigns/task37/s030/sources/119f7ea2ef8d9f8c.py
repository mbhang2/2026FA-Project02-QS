"""
Trial #14 achieved +0.9686 with volatility-adaptive entry filters. Analysis of
remaining optimization opportunities:

The strategy currently uses:
1. Fast MA (6) vs Slow MA (20) for trend detection
2. Volatility-scaled stops/targets with asymmetric multipliers (stop=1.15, target=1.40)
3. Adaptive bar strength threshold based on volatility regime
4. Adaptive price tolerance band based on volatility
5. MA divergence threshold of 0.00145

Key observations from trial progression:
- Trial #12 (+0.9941) had simpler, tighter parameters: vol_mult_stop=1.2, vol_mult_target=1.35
- Trial #13 (+1.0353) improved with slightly looser stops/targets and price tolerance
- Trial #14 (+1.0260) regressed slightly despite adaptive filters

The regression suggests over-optimization. Trial #13's +1.0353 is the best score,
achieved with:
- vol_multiplier_stop=1.18, vol_multiplier_target=1.38
- min_bar_strength_threshold=0.38
- min_ma_divergence=0.00148
- Static (non-adaptive) price tolerance=0.001

For trial #15, I'll refine trial #13's winning formula:
1. Slightly tighter MA divergence (0.00146) - find sweet spot between #12 and #13
2. Reduce bar_strength_threshold to 0.37 (catch slightly more signals)
3. Increase vol_multiplier_target to 1.40 (capture bigger wins)
4. Adjust vol_multiplier_stop to 1.16 (better asymmetry)
5. Add a secondary filter: require price to be within 0.5% of fast MA for entries
   (This adds conviction without over-filtering)
6. Slightly increase max_target to 2.8 (allow bigger wins on strong moves)

The key insight: Trial #13 worked because it had the right balance between
strictness and permissiveness. We should make micro-adjustments to that formula
rather than adding complex adaptive logic that seems to hurt performance.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 13,
                 vol_multiplier_stop: float = 1.16,
                 vol_multiplier_target: float = 1.40,
                 min_stop: float = 0.68, max_stop: float = 1.5,
                 min_target: float = 0.9, max_target: float = 2.8,
                 min_ma_divergence: float = 0.00146,
                 bar_strength_threshold: float = 0.37,
                 price_tolerance: float = 0.001,
                 price_proximity_threshold: float = 0.005,
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
        self.price_proximity_threshold = price_proximity_threshold
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
        
        # Check bar strength
        if bar_strength < self.bar_strength_threshold:
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Price proximity check: price should be within proximity_threshold of fast MA
        price_distance_ratio = abs(current_price - fast_ma) / fast_ma if fast_ma > 0 else 1.0
        
        # Buy: uptrend (fast > slow) + price confirmation + bar closed up + bar has strength
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            price_distance_ratio <= self.price_proximity_threshold and
            last_bar.close > last_bar.open and bar_strength >= self.bar_strength_threshold):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price confirmation + bar closed down + bar has strength
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              price_distance_ratio <= self.price_proximity_threshold and
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
