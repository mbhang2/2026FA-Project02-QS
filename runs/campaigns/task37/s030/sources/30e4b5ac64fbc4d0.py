"""
Analysis of trials #0-#73 and strategic refinement:

Best score: #63 at +1.5421 with 681 trades.

Key observations:
1. Top performers (#56, #63, #67, #70, #71) cluster around +1.54 with 680-700 trades
2. Trial #72 regressed to +1.4682 when we introduced strong_ma_divergence threshold
3. Trial #73 returned to percentile-based volatility but scored +1.4743 (680 trades)
4. The core MA crossover + volatility scaling + bar strength signal is proven

Critical insight - ENTRY TIMING PRECISION:
The plateau at ~+1.54 suggests we've optimized exit sizing well. The frontier is
entry timing precision. Current approach uses:
- MA crossover direction (fast > slow or <)
- Bar strength threshold (0.28)
- Price tolerance band
- MA divergence minimum (0.00135)

But we're missing a critical filter: MOMENTUM CONFIRMATION.
What if we require not just that the bar closed in the direction of the signal,
but that it also showed ACCELERATION in that direction?

New hypothesis - MOMENTUM-CONFIRMED ENTRIES:
1. Keep the proven entry framework (MA crossover + bar strength + price tolerance)
2. Add momentum confirmation: require the current bar's body to be LARGER than
   the previous bar's body (for the same direction)
3. This filters out exhaustion bars and increases entry quality
4. Slightly reduce bar_strength_threshold to 0.26 (momentum filter is now the gate)
5. Slightly reduce min_ma_divergence to 0.00130 (momentum filter compensates)
6. Adjust volatility multipliers back to #71's proven values (1.15/1.60 and 0.95/1.40)
7. Keep vol_history_depth at 80 for fast regime adaptation

The insight: We're not changing the core signal, we're adding a confirmation that
filters out false breakouts. Bars that close in the trend direction but with
SMALLER bodies than the previous bar are often reversals - momentum confirmation
prevents trading these.

Expected outcome: Score at or above +1.5421 through more precise entry timing
that filters reversals while maintaining the proven exit strategy.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_history_depth: int = 80,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00130,
                 bar_strength_threshold: float = 0.26,
                 price_tolerance: float = 0.0020,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.vol_history_depth = vol_history_depth
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
        # Accumulate true ranges for volatility
        self.true_ranges: list[float] = []
        # Track previous close for true range calculation
        self.prev_close: float | None = None
        # Track volatility history for percentile calculation
        self.vol_history: list[float] = []
        # Track bar bodies for momentum confirmation
        self.bar_bodies: list[float] = []

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
        
        # Calculate and track bar body
        bar_body = abs(last_bar.close - last_bar.open)
        self.bar_bodies.append(bar_body)
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + 10
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.true_ranges) > max_needed:
            self.true_ranges = self.true_ranges[-max_needed:]
        if len(self.bar_bodies) > max_needed:
            self.bar_bodies = self.bar_bodies[-max_needed:]

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
        
        # Determine volatility regime using percentiles
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
            # else: normal volatility uses defaults above
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        bar_strength = bar_body / bar_range
        
        # Bar strength must meet threshold
        if bar_strength < self.bar_strength_threshold:
            return
        
        # MOMENTUM CONFIRMATION: Current bar body must be >= previous bar body
        # This filters out exhaustion bars and false breakouts
        has_momentum = True
        if len(self.bar_bodies) >= 2:
            prev_body = self.bar_bodies[-2]
            if bar_body < prev_body:
                has_momentum = False
        
        if not has_momentum:
            return
        
        # Use volatility-aware scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * vol_mult_stop)
        vol_adjustment_target = 1.0 + (vol * vol_mult_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up + momentum
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down + momentum
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
