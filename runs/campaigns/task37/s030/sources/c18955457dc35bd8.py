"""
Analysis of trials #0-#74 and strategic breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the progression:
1. Top performers (#56, #63, #67, #70, #71) cluster around +1.54 with 680-700 trades
2. Trial #74 added momentum confirmation (current bar body >= previous bar body)
   but scored +0.9837 with only 560 trades - TOO RESTRICTIVE
3. The momentum filter eliminated too many valid entries
4. Core insight: The entry selectivity is already optimized; we need to look at
   EXIT optimization instead

Critical insight - EXIT OPTIMIZATION IS THE FRONTIER:
The plateau at ~+1.54 suggests entry timing is mature. But exit sizing is still
mechanical. What if we:

1. Keep the proven entry framework (MA crossover + bar strength + price tolerance)
2. REMOVE the momentum confirmation (it was too restrictive)
3. Add ADAPTIVE EXIT SIZING based on:
   - Distance from entry to next support/resistance (measured from recent swings)
   - Trend strength (how far fast MA is from slow MA as a % of slow MA)
   - Entry bar's position in the range (did we enter early or late in the move?)

The hypothesis: Instead of fixed vol_mult_stop and vol_mult_target, use:
- Swing-based targets: measure recent swings (highs/lows of last 10 bars),
  place target at the next swing high/low in the direction of trade
- Trend-strength scaling: if fast MA is far from slow MA, we're in a strong trend,
  use wider brackets; if close, use tighter brackets
- Entry position scaling: if we entered near the low of the bar (for buys),
  we can afford wider stops; if near the high, use tighter stops

This keeps the number of trades high (no filtering) while improving exit quality
through smarter bracket placement.

Expected outcome: Score at or above +1.5421 through adaptive exit sizing that
responds to market structure rather than just volatility percentiles.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 swing_lookback: int = 10, vol_history_depth: int = 80,
                 min_stop: float = 0.60, max_stop: float = 1.8,
                 min_target: float = 0.80, max_target: float = 3.50,
                 min_ma_divergence: float = 0.00130,
                 bar_strength_threshold: float = 0.25,
                 price_tolerance: float = 0.0020,
                 trend_strength_factor: float = 0.8,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.swing_lookback = swing_lookback
        self.vol_history_depth = vol_history_depth
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
        self.bar_strength_threshold = bar_strength_threshold
        self.price_tolerance = price_tolerance
        self.trend_strength_factor = trend_strength_factor
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for volatility
        self.true_ranges: list[float] = []
        # Track previous close for true range calculation
        self.prev_close: float | None = None
        # Track volatility history for percentile calculation
        self.vol_history: list[float] = []
        # Track recent highs and lows for swing detection
        self.recent_highs: list[float] = []
        self.recent_lows: list[float] = []

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
        
        # Track recent highs and lows for swing-based exits
        self.recent_highs.append(last_bar.high)
        self.recent_lows.append(last_bar.low)
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + self.swing_lookback + 10
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.true_ranges) > max_needed:
            self.true_ranges = self.true_ranges[-max_needed:]
        if len(self.recent_highs) > max_needed:
            self.recent_highs = self.recent_highs[-max_needed:]
        if len(self.recent_lows) > max_needed:
            self.recent_lows = self.recent_lows[-max_needed:]

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
        
        # Track volatility history for percentile calculation
        self.vol_history.append(vol)
        if len(self.vol_history) > self.vol_history_depth:
            self.vol_history = self.vol_history[-self.vol_history_depth:]
        
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
        
        # Bar strength must meet threshold
        if bar_strength < self.bar_strength_threshold:
            return
        
        # Calculate trend strength: how far is fast MA from slow MA?
        trend_strength = ma_divergence / 0.01 if ma_divergence > 0 else 0
        trend_strength = min(trend_strength, 2.0)  # Cap at 2.0
        
        # Calculate adaptive stop and target based on:
        # 1. Volatility regime (percentile-based)
        # 2. Trend strength (distance between MAs)
        # 3. Entry bar position in range
        
        # Base multipliers from volatility regime
        vol_mult_stop = 1.08
        vol_mult_target = 1.52
        
        if len(self.vol_history) >= 20:
            sorted_vols = sorted(self.vol_history)
            vol_25_idx = len(sorted_vols) // 4
            vol_75_idx = 3 * len(sorted_vols) // 4
            vol_25 = sorted_vols[vol_25_idx] if vol_25_idx < len(sorted_vols) else 0
            vol_75 = sorted_vols[vol_75_idx] if vol_75_idx < len(sorted_vols) else 0
            
            if vol > vol_75:
                # High volatility: wider brackets
                vol_mult_stop = 1.15
                vol_mult_target = 1.60
            elif vol < vol_25:
                # Low volatility: tighter brackets
                vol_mult_stop = 0.95
                vol_mult_target = 1.40
        
        # Adjust multipliers based on trend strength
        # Strong trend: use wider brackets; weak trend: use tighter brackets
        trend_adjustment = 0.5 + (trend_strength * self.trend_strength_factor * 0.25)
        vol_mult_stop = vol_mult_stop * trend_adjustment
        vol_mult_target = vol_mult_target * trend_adjustment
        
        # Adjust based on entry bar position in range
        # For buys: if close is near high, we're late in the move, tighten stop
        # For sells: if close is near low, we're late in the move, tighten stop
        if bar_range > 0:
            entry_position = (last_bar.close - last_bar.low) / bar_range
        else:
            entry_position = 0.5
        
        # Entry position adjustment: 0.5 is neutral, 0.0 means very early, 1.0 means very late
        position_adjustment = 0.7 + (entry_position * 0.6)  # Range: 0.7 to 1.3
        vol_mult_stop = vol_mult_stop * position_adjustment
        vol_mult_target = vol_mult_target * position_adjustment
        
        # Use volatility-aware scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * vol_mult_stop)
        vol_adjustment_target = 1.0 + (vol * vol_mult_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band
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
