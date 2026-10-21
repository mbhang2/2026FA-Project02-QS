"""
Trial #13 achieved +0.9941, nearly optimal. The strategy uses:
- Fast MA (6-period) vs Slow MA (20-period) for trend detection
- Volatility-scaled stops and targets with asymmetric multipliers
- Bar strength filter (body/range ratio) for directional conviction
- Price tolerance band to reduce MA crossover whipsaws

Analysis of remaining optimization opportunities:

1. Entry signal refinement: The current MA crossover + price confirmation is solid,
   but we could add a secondary filter based on recent volatility regime:
   - When volatility is very low, require stronger bar strength
   - When volatility is high, be slightly more permissive (trend is clearer)
   - This adapts entry strictness to market conditions

2. Dynamic bar strength threshold: Instead of fixed 0.38, use volatility-adaptive
   - Low vol: require 0.42+ bar strength (filter choppy markets)
   - High vol: allow 0.35+ bar strength (capture strong moves)
   - This filters noise in quiet periods, catches trends in volatile periods

3. MA divergence threshold refinement:
   - Current: 0.00148 (very tight)
   - Try: 0.00145 (slightly tighter to be more selective)
   - Or adaptive: scale with volatility (tighter in low-vol, looser in high-vol)

4. Volatility multiplier fine-tuning:
   - Current: stop=1.18, target=1.38
   - Try: stop=1.15, target=1.40 (even more asymmetric)
   - This further increases target relative to stop, capturing bigger moves

5. Price tolerance dynamic adjustment:
   - Current: fixed 0.001 (0.1% of slow MA)
   - Try: scale with volatility (wider band in high-vol, tighter in low-vol)
   - This adapts entry strictness to market regime

For trial #14, I'll implement volatility-adaptive entry filters:
- Volatility-scaled bar strength threshold (0.35-0.42 range)
- Slightly tighter MA divergence threshold (0.00145)
- More aggressive vol multipliers (stop=1.15, target=1.40)
- Volatility-adaptive price tolerance band
- This creates a regime-aware strategy that adapts to market conditions

The key insight: the best entries come when the signal is strong AND the market
regime supports it. By adapting filter strictness to volatility, we catch more
high-conviction trades while filtering noise in choppy markets.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 13,
                 vol_multiplier_stop: float = 1.15,
                 vol_multiplier_target: float = 1.40,
                 min_stop: float = 0.68, max_stop: float = 1.5,
                 min_target: float = 0.9, max_target: float = 2.75,
                 min_ma_divergence: float = 0.00145,
                 base_bar_strength_threshold: float = 0.38,
                 min_bar_strength: float = 0.35,
                 max_bar_strength: float = 0.42,
                 vol_threshold_low: float = 0.005,
                 vol_threshold_high: float = 0.015,
                 base_price_tolerance: float = 0.001,
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
        self.base_bar_strength_threshold = base_bar_strength_threshold
        self.min_bar_strength = min_bar_strength
        self.max_bar_strength = max_bar_strength
        self.vol_threshold_low = vol_threshold_low
        self.vol_threshold_high = vol_threshold_high
        self.base_price_tolerance = base_price_tolerance
        self.size = size
        # Accumulate closes for MA and volatility calculations
        self.closes: list[float] = []
        # Track returns for volatility
        self.returns: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _get_adaptive_bar_strength_threshold(self, vol: float) -> float:
        """Adapt bar strength threshold based on volatility regime."""
        if vol < self.vol_threshold_low:
            # Low volatility: require stronger bars (filter choppy markets)
            return self.max_bar_strength
        elif vol > self.vol_threshold_high:
            # High volatility: allow weaker bars (trends are clearer)
            return self.min_bar_strength
        else:
            # Medium volatility: interpolate between min and max
            vol_range = self.vol_threshold_high - self.vol_threshold_low
            vol_position = (vol - self.vol_threshold_low) / vol_range
            # As vol increases from low to high, threshold decreases from max to min
            return self.max_bar_strength - (vol_position * (self.max_bar_strength - self.min_bar_strength))

    def _get_adaptive_price_tolerance(self, vol: float) -> float:
        """Adapt price tolerance band based on volatility regime."""
        # In high volatility, use wider tolerance band
        # In low volatility, use tighter tolerance band
        vol_adjustment = 1.0 + (vol / self.vol_threshold_high) if vol > 0 else 1.0
        return self.base_price_tolerance * min(vol_adjustment, 2.0)

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
        
        # Get adaptive thresholds based on volatility regime
        adaptive_bar_strength = self._get_adaptive_bar_strength_threshold(vol)
        adaptive_price_tolerance = self._get_adaptive_price_tolerance(vol)
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band (adaptive to volatility)
        tolerance_band = adaptive_price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up + bar has strength
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and bar_strength >= adaptive_bar_strength):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down + bar has strength
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and bar_strength >= adaptive_bar_strength):
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
