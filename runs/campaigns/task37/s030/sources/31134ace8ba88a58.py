"""
Analysis of trials #51-#60 and strategic direction for breakthrough:

Best score: #56 at +1.5407 with 683 trades.

Key observations from the progression:
1. Core signal (MA crossover + ATR volatility scaling) is proven and stable
2. All attempts to add filters or complexity reduce score
3. Micro-parameter tuning shows we're at a local optimum around +1.54
4. Adding prior bar momentum (#59) was a step backward
5. Tightening thresholds (#60) also regressed

Critical insight: The plateau suggests the current MA crossover approach has
reached its natural limit. The signal is already well-optimized. To break through,
we need to reconsider what we're trading.

Current approach trades on:
- 6-bar vs 20-bar MA crossover
- ATR-based volatility scaling
- Bar strength confirmation
- Price tolerance band

The issue: We're trading the same signal repeatedly. What if instead of trying
to optimize the SAME entry signal, we need to ADD A COMPLEMENTARY signal that
catches different types of moves?

New hypothesis - MEAN REVERSION COMPLEMENT:
The MA crossover captures trend continuation well. But markets also have
mean-reversion moves, especially after extreme bars. What if we:

1. Keep the MA crossover signal as-is (it works at +1.54)
2. Add a COMPLEMENTARY mean-reversion signal:
   - Detect extreme bars (bar_strength > 0.70 in opposite direction)
   - Check if price has moved too far from the slow MA
   - Enter a contrarian position with smaller size
   - Use tighter stops for these high-risk entries

3. ADAPTIVE POSITION SIZING:
   - MA crossover entries: full size (proven signal)
   - Mean reversion entries: 0.5x size (experimental signal)
   - This lets us test the hypothesis without blowing up the score

4. VOLATILITY-ADJUSTED ENTRY TIMING:
   - In high volatility: be less selective (lower bar_strength threshold)
   - In low volatility: be more selective (higher bar_strength threshold)
   - This adapts the signal to market regime

Expected outcome: The MA crossover keeps its +1.54 baseline, and the mean
reversion signal either adds value (pushing us past +1.54) or adds noise
(keeping us around +1.54). Either way, we learn whether the approach is viable.

Implementation: Keep #56's proven parameters, add the mean reversion logic
with small position size, and adaptive volatility-based selectivity.
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
                 bar_strength_threshold: float = 0.32,
                 price_tolerance: float = 0.0021,
                 extreme_bar_threshold: float = 0.70,
                 mean_reversion_distance: float = 0.0030,
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
        self.extreme_bar_threshold = extreme_bar_threshold
        self.mean_reversion_distance = mean_reversion_distance
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for ATR-like volatility
        self.true_ranges: list[float] = []
        # Track previous close for true range calculation
        self.prev_close: float | None = None

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
            return

        # Calculate fast and slow moving averages
        fast_closes = self.closes[-self.fast_lookback:]
        slow_closes = self.closes[-self.slow_lookback:]
        
        fast_ma = sum(fast_closes) / len(fast_closes)
        slow_ma = sum(slow_closes) / len(slow_closes)
        
        if slow_ma <= 0:
            return
        
        # Calculate volatility using ATR (average true range)
        vol = 0.0
        if len(self.true_ranges) >= self.vol_lookback:
            recent_ranges = self.true_ranges[-self.vol_lookback:]
            atr = sum(recent_ranges) / len(recent_ranges)
            # Normalize by current price to get volatility as a fraction
            if current_price > 0:
                vol = atr / current_price
        
        # Calculate volatility regime for adaptive selectivity
        vol_median = 0.0
        if len(self.true_ranges) >= self.vol_lookback * 2:
            longer_ranges = self.true_ranges[-(self.vol_lookback * 2):]
            if current_price > 0:
                vols = [tr / current_price for tr in longer_ranges]
                vols_sorted = sorted(vols)
                vol_median = vols_sorted[len(vols_sorted) // 2]
        
        # Adaptive bar strength threshold based on volatility regime
        adaptive_bar_strength = self.bar_strength_threshold
        if vol_median > 0:
            vol_ratio = vol / vol_median
            if vol_ratio < 0.75:  # Low volatility - be more selective
                adaptive_bar_strength = self.bar_strength_threshold + 0.04
            elif vol_ratio > 1.25:  # High volatility - be less selective
                adaptive_bar_strength = max(0.25, self.bar_strength_threshold - 0.04)
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
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
        
        # SIGNAL 1: MA CROSSOVER (proven signal - keep as-is)
        if ma_divergence >= self.min_ma_divergence and bar_strength >= adaptive_bar_strength:
            if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
                last_bar.close > last_bar.open):
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-long",
                )
                return
            elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
                  last_bar.close < last_bar.open):
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-short",
                )
                return
        
        # SIGNAL 2: MEAN REVERSION (complementary signal - smaller size)
        # Only trade mean reversion if MA divergence is weak (not in strong trend)
        if ma_divergence < self.min_ma_divergence * 2:
            distance_from_slow_ma = abs(current_price - slow_ma) / slow_ma
            
            # Detect extreme bars moving away from slow MA
            if bar_strength >= self.extreme_bar_threshold:
                # Bar moved up strongly - potential reversion down
                if (last_bar.close > last_bar.open and 
                    current_price > slow_ma + (self.mean_reversion_distance * slow_ma)):
                    ctx.sell(
                        size=max(1, self.size // 2),
                        stop_offset=stop_offset * 1.2,  # Wider stop for mean reversion
                        target_offset=target_offset * 0.8,  # Tighter target
                        tag="mean-rev-short",
                    )
                # Bar moved down strongly - potential reversion up
                elif (last_bar.close < last_bar.open and 
                      current_price < slow_ma - (self.mean_reversion_distance * slow_ma)):
                    ctx.buy(
                        size=max(1, self.size // 2),
                        stop_offset=stop_offset * 1.2,  # Wider stop for mean reversion
                        target_offset=target_offset * 0.8,  # Tighter target
                        tag="mean-rev-long",
                    )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
