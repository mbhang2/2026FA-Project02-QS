"""
Analysis of trials #51-#53 and breakthrough optimization:

Best score: #51 at +1.5208 with 682 trades.
Recent: #53 at +1.5206 with 682 trades (essentially identical to #51).

Key observations from the data:
1. #51: +1.5208 (682 trades) - volatility multipliers: stop=1.13, target=1.47
2. #52: +0.5561 (580 trades) - adding close_position_in_bar filter was destructive
3. #53: +1.5206 (682 trades) - micro-tuning parameters around #51 yields no improvement

The plateau at +1.52 with 680-682 trades suggests we're near a local optimum with
the current approach. The strategy works because:
- MA crossover signal is clean and effective
- ATR-based volatility scaling adapts to market conditions
- Minimal filters preserve trade quality
- Tight stops (1.13) and higher targets (1.47) optimize risk/reward

To break through, we need a different angle. Current hypothesis:
1. The MA divergence threshold (0.00142) may be too tight - we're missing some
   valid crossovers that would be profitable
2. The bar_strength_threshold (0.33) filters out some valid momentum trades
3. The price_tolerance band (0.0020) may be too wide - causing some whipsaws

For trial #54, try:
1. Slightly loosen min_ma_divergence to 0.00138 (catch more valid crossovers)
2. Lower bar_strength_threshold to 0.30 (allow slightly weaker bars if MA signal is strong)
3. Tighten price_tolerance to 0.0015 (reduce whipsaws)
4. Adjust vol_multiplier_stop to 1.14 (slightly wider stops for better fills)
5. Adjust vol_multiplier_target to 1.46 (keep targets similar)
6. Add a subtle trend strength check: require fast MA to be moving in the direction
   of the signal (check if fast_ma is moving away from slow_ma)

The key insight: #51 proved the core model works. Now we need to:
- Catch MORE valid entries (loosen MA divergence, lower bar strength)
- Reduce whipsaws (tighter price tolerance)
- Add subtle trend strength confirmation without being restrictive
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.14,
                 vol_multiplier_target: float = 1.46,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00138,
                 bar_strength_threshold: float = 0.30,
                 price_tolerance: float = 0.0015,
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
        # Track previous fast MA for trend strength
        self.prev_fast_ma: float | None = None

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
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            self.prev_fast_ma = fast_ma
            return
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            self.prev_fast_ma = fast_ma
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        if bar_strength < self.bar_strength_threshold:
            self.prev_fast_ma = fast_ma
            return
        
        # Check trend strength: fast MA should be moving away from slow MA
        trend_strength_ok = True
        if self.prev_fast_ma is not None:
            prev_divergence = abs(self.prev_fast_ma - slow_ma) / slow_ma
            # Only trade if divergence is increasing (trend strengthening)
            if ma_divergence <= prev_divergence:
                trend_strength_ok = False
        
        if not trend_strength_ok:
            self.prev_fast_ma = fast_ma
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
        
        self.prev_fast_ma = fast_ma

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
