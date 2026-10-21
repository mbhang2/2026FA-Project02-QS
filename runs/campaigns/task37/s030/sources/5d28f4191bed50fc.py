"""
Analysis of trials #0-#76 and strategic refinement for next breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers (#56, #63, #67, #70, #71) cluster around +1.54 with 680-700 trades
2. Trial #75 (adaptive exit sizing) regressed to +0.7127 - too complex
3. Trial #76 (range-based confirmation) regressed to +0.7127 - still too restrictive
4. The core MA crossover + volatility scaling + bar strength signal from #63 is proven

Critical insight - ENTRY TIMING REFINEMENT WITH PULLBACK DETECTION:
The plateau at ~+1.54 suggests we've optimized basic MA crossover. But we're
still getting entries that are too late in a move. What if we:

1. Keep the proven entry framework from #63 (MA crossover + bar strength + price tolerance)
2. REMOVE the range-based confirmation (it was too restrictive, #76 scored +0.7127)
3. Add a PULLBACK FILTER: Only enter if the last bar pulled back from the previous bar
   - For buys: last bar's low < previous bar's low (pullback confirmation)
   - For sells: last bar's high > previous bar's high (pullback confirmation)
   This ensures we're entering on strength AFTER a pullback, not chasing moves
4. Keep exact parameters from #63 that scored +1.5421

The hypothesis: #63 was strong because it had good entry selectivity through
bar strength and MA divergence. But some entries were still on bars that continued
a strong move without pullback. A pullback filter adds conviction without being
overly restrictive like range confirmation was.

Expected outcome: Match or exceed #63's +1.5421 by filtering for pullback entries
that have higher probability of continuation, maintaining trade frequency while
improving quality.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.10,
                 vol_multiplier_target: float = 1.50,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00140,
                 bar_strength_threshold: float = 0.32,
                 bar_strength_low_vol: float = 0.25,
                 bar_strength_high_vol: float = 0.40,
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
        self.bar_strength_low_vol = bar_strength_low_vol
        self.bar_strength_high_vol = bar_strength_high_vol
        self.price_tolerance = price_tolerance
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for ATR-like volatility
        self.true_ranges: list[float] = []
        # Track previous bar for pullback detection
        self.prev_bar_high: float | None = None
        self.prev_bar_low: float | None = None
        # Track previous close for true range calculation
        self.prev_close: float | None = None
        # Track volatility history for regime detection
        self.vol_history: list[float] = []

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
        max_needed = self.slow_lookback + self.vol_lookback + 10
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.true_ranges) > max_needed:
            self.true_ranges = self.true_ranges[-max_needed:]

        # Need enough data for both moving averages
        if len(self.closes) < self.slow_lookback:
            # Store bar info for next iteration
            self.prev_bar_high = last_bar.high
            self.prev_bar_low = last_bar.low
            return

        # Calculate fast and slow moving averages
        fast_closes = self.closes[-self.fast_lookback:]
        slow_closes = self.closes[-self.slow_lookback:]
        
        fast_ma = sum(fast_closes) / len(fast_closes)
        slow_ma = sum(slow_closes) / len(slow_closes)
        
        if slow_ma <= 0:
            self.prev_bar_high = last_bar.high
            self.prev_bar_low = last_bar.low
            return
        
        # Calculate volatility using ATR (average true range)
        vol = 0.0
        if len(self.true_ranges) >= self.vol_lookback:
            recent_ranges = self.true_ranges[-self.vol_lookback:]
            atr = sum(recent_ranges) / len(recent_ranges)
            # Normalize by current price to get volatility as a fraction
            if current_price > 0:
                vol = atr / current_price
        
        self.vol_history.append(vol)
        if len(self.vol_history) > self.vol_lookback * 2:
            self.vol_history = self.vol_history[-(self.vol_lookback * 2):]
        
        # Calculate volatility regime (median of recent volatility)
        vol_regime = 0.0
        if len(self.vol_history) >= self.vol_lookback:
            vol_sorted = sorted(self.vol_history[-self.vol_lookback:])
            vol_regime = vol_sorted[len(vol_sorted) // 2]
        
        # Adapt bar strength threshold based on volatility regime
        adaptive_bar_strength = self.bar_strength_threshold
        if vol_regime > 0:
            vol_ratio = vol / vol_regime if vol_regime > 0 else 1.0
            # High volatility (vol_ratio > 1.2): be less selective
            if vol_ratio > 1.2:
                adaptive_bar_strength = self.bar_strength_low_vol
            # Low volatility (vol_ratio < 0.8): be more selective
            elif vol_ratio < 0.8:
                adaptive_bar_strength = self.bar_strength_high_vol
            else:
                # Normal regime: interpolate between thresholds
                adaptive_bar_strength = (self.bar_strength_low_vol + self.bar_strength_high_vol) / 2
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            self.prev_bar_high = last_bar.high
            self.prev_bar_low = last_bar.low
            return
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            self.prev_bar_high = last_bar.high
            self.prev_bar_low = last_bar.low
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        if bar_strength < adaptive_bar_strength:
            self.prev_bar_high = last_bar.high
            self.prev_bar_low = last_bar.low
            return
        
        # PULLBACK CONFIRMATION: Check if current bar pulled back from previous bar
        # For buys: current low < previous low (pullback after strength)
        # For sells: current high > previous high (pullback after weakness)
        pullback_confirmed = False
        if self.prev_bar_high is not None and self.prev_bar_low is not None:
            # For buy signals: need pullback (lower low)
            if fast_ma > slow_ma and last_bar.close > last_bar.open:
                if last_bar.low < self.prev_bar_low:
                    pullback_confirmed = True
            # For sell signals: need pullback (higher high)
            elif fast_ma < slow_ma and last_bar.close < last_bar.open:
                if last_bar.high > self.prev_bar_high:
                    pullback_confirmed = True
        else:
            # First bar, no previous to compare, allow entry
            pullback_confirmed = True
        
        if not pullback_confirmed:
            self.prev_bar_high = last_bar.high
            self.prev_bar_low = last_bar.low
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up + pullback
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down + pullback
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open):
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-short",
            )
        
        # Store current bar info for next iteration
        self.prev_bar_high = last_bar.high
        self.prev_bar_low = last_bar.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
