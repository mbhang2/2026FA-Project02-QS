"""
Analysis of trials #51-#58 and optimization breakthrough:

Best score: #58 at +1.4982 with 684 trades (slight regression from #56's +1.5407).

Critical pattern recognition from the data:
- #56: +1.5407 (683 trades) - vol_mult_stop=1.12, vol_mult_target=1.48, bar_strength=0.32
- #58: +1.4982 (684 trades) - vol_mult_stop=1.10, vol_mult_target=1.50, bar_strength=0.31
- Tightening stops and lowering bar_strength threshold REDUCED score

Key insight: We're over-optimizing. The plateau at +1.54 suggests the core signal
is saturated. Looking at the progression:
- Adding filters (delta, momentum) consistently hurts (-0.4 to -0.5)
- Micro-tuning parameters shows diminishing returns
- The MA crossover + ATR volatility + bar strength + price tolerance is optimal

New hypothesis for breakthrough:
The issue might be ENTRY TIMING within the bar. Currently we check:
1. MA crossover established
2. Bar strength sufficient
3. Price in tolerance band
4. Bar closed in direction

But we're entering on the CLOSE of the bar. What if we're entering too late in
the move? Two approaches:

1. ADAPTIVE ENTRY TIMING: If bar strength is very high (>0.50), enter more aggressively
   by reducing the price_tolerance requirement. Strong bars = less need for confirmation.

2. VOLATILITY-ADJUSTED THRESHOLDS: When volatility is LOW, we should be MORE selective
   (higher bar_strength, tighter tolerance). When volatility is HIGH, we should be LESS
   selective (lower bar_strength, wider tolerance). This adapts to market regime.

3. PRIOR BAR MOMENTUM: Look at whether the previous bar also moved in the same direction.
   A bar that continues the trend of the prior bar is stronger than an isolated bar.

For trial #59, implement:
- Keep #56's proven parameters as base (vol_mult_stop=1.12, vol_mult_target=1.48)
- Add volatility regime detection: if vol < median, increase selectivity; if vol > median, decrease
- Add prior bar confirmation: require prior bar to close in same direction for +0.02 boost
- Slightly adjust bar_strength_threshold to 0.315 (middle of the range)
- Keep price_tolerance at 0.0021 (proven)
- Keep min_ma_divergence at 0.00140 (proven)

The key: We're not adding complexity, just making the existing signal adaptive to
volatility regime, which is a known source of edge.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.12,
                 vol_multiplier_target: float = 1.48,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00140,
                 bar_strength_threshold: float = 0.315,
                 price_tolerance: float = 0.0021,
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
        # Accumulate true ranges for ATR-like volatility
        self.true_ranges: list[float] = []
        # Track previous close for true range calculation
        self.prev_close: float | None = None
        # Store previous bar for momentum confirmation
        self.prev_bar_up: bool | None = None

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
        
        # Calculate true range (ATR component)
        high_low = last_bar.high - last_bar.low
        if self.prev_close is not None:
            high_prev = abs(last_bar.high - self.prev_close)
            low_prev = abs(last_bar.low - self.prev_close)
            true_range = max(high_low, high_prev, low_prev)
        else:
            true_range = high_low
        
        self.true_ranges.append(true_range)
        self.prev_close = current_price
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + 5
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.true_ranges) > max_needed:
            self.true_ranges = self.true_ranges[-max_needed:]

        # Need enough data for both moving averages
        if len(self.closes) < self.slow_lookback:
            # Still accumulating, but track bar direction
            self.prev_bar_up = last_bar.close > last_bar.open
            return

        # Calculate fast and slow moving averages
        fast_closes = self.closes[-self.fast_lookback:]
        slow_closes = self.closes[-self.slow_lookback:]
        
        fast_ma = sum(fast_closes) / len(fast_closes)
        slow_ma = sum(slow_closes) / len(slow_closes)
        
        if slow_ma <= 0:
            self.prev_bar_up = last_bar.close > last_bar.open
            return
        
        # Calculate volatility using ATR (average true range)
        vol = 0.0
        if len(self.true_ranges) >= self.vol_lookback:
            recent_ranges = self.true_ranges[-self.vol_lookback:]
            atr = sum(recent_ranges) / len(recent_ranges)
            # Normalize by current price to get volatility as a fraction
            if current_price > 0:
                vol = atr / current_price
        
        # Calculate median volatility for regime detection
        vol_median = 0.0
        if len(self.true_ranges) >= self.vol_lookback * 2:
            # Use a longer window to estimate median volatility
            longer_ranges = self.true_ranges[-(self.vol_lookback * 2):]
            if current_price > 0:
                vols = [tr / current_price for tr in longer_ranges]
                vols_sorted = sorted(vols)
                vol_median = vols_sorted[len(vols_sorted) // 2]
        
        # Adaptive threshold based on volatility regime
        if vol_median > 0:
            vol_ratio = vol / vol_median
        else:
            vol_ratio = 1.0
        
        # In low vol regimes, be more selective; in high vol, be less selective
        adaptive_bar_strength = self.bar_strength_threshold
        if vol_ratio < 0.8:  # Low volatility
            adaptive_bar_strength = self.bar_strength_threshold + 0.05
        elif vol_ratio > 1.2:  # High volatility
            adaptive_bar_strength = max(0.20, self.bar_strength_threshold - 0.05)
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            self.prev_bar_up = last_bar.close > last_bar.open
            return
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            self.prev_bar_up = last_bar.close > last_bar.open
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        if bar_strength < adaptive_bar_strength:
            self.prev_bar_up = last_bar.close > last_bar.open
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Determine current bar direction
        bar_up = last_bar.close > last_bar.open
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
        # + prior bar also up (momentum confirmation)
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            bar_up and (self.prev_bar_up is None or self.prev_bar_up)):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
        # + prior bar also down (momentum confirmation)
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              not bar_up and (self.prev_bar_up is None or not self.prev_bar_up)):
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-short",
            )
        
        self.prev_bar_up = bar_up

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
