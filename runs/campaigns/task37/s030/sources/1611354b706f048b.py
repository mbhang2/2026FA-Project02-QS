"""
Analysis of trials #0-#70 and strategic refinement:

Best score: #63 at +1.5421 with 681 trades.

Key observation: All top performers cluster around +1.54 with 680-700 trades.
The core MA crossover + volatility scaling is proven. The plateau suggests
we've optimized entry selectivity well, but may be leaving money on the table
with exit timing.

Critical insight from recent trials:
- #69 and #70 show that tighter bar_strength thresholds (0.25-0.28) perform well
- The price_tolerance band (0.0020-0.0025) is effective for entry timing
- The volatility multipliers are well-tuned (stop ~1.08, target ~1.52)
- BUT: We're using fixed offsets for ALL trades regardless of context

New hypothesis - CONTEXT-AWARE EXIT SIZING:
What if the issue isn't the entry signal or the bracket parameters, but rather
that we're using one-size-fits-all stop/target offsets?

Observation: In #63, we had adaptive bar_strength based on volatility regime.
This was abandoned in #69/#70 for simplicity. But what if we bring back
ADAPTIVE VOLATILITY SCALING without the complexity?

Specifically:
1. Keep the proven entry signal (MA crossover + bar strength + price tolerance)
2. Calculate a "volatility percentile" (where does current vol rank historically?)
3. Use this to scale the stop/target offsets:
   - High vol (75th+ percentile): Wider brackets (stop 1.15, target 1.60)
   - Normal vol (25-75th): Standard brackets (stop 1.08, target 1.52)
   - Low vol (below 25th): Tighter brackets (stop 0.95, target 1.40)
4. This adapts risk management to market regime WITHOUT adding entry filters

The insight: In high volatility, we need wider brackets to avoid whipsaws.
In low volatility, tighter brackets lock in gains faster. This should improve
the risk/reward profile without changing trade count significantly.

Also: Slightly increase min_ma_divergence to 0.00130 (from 0.00120) to be
more selective on marginal crossovers.

Expected outcome: Score at or above +1.5421 with potentially better risk metrics
through adaptive volatility-aware exit sizing.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_history_depth: int = 100,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00130,
                 bar_strength_threshold: float = 0.25,
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
        
        # Calculate volatility percentile
        vol_percentile = 0.5  # default to median
        if len(self.vol_history) >= 10:
            sorted_vols = sorted(self.vol_history)
            vol_percentile = sorted_vols[len(sorted_vols) // 2] / (max(sorted_vols) + 1e-10)
        
        # Determine volatility regime
        if len(self.vol_history) >= 20:
            sorted_vols = sorted(self.vol_history)
            vol_25 = sorted_vols[len(sorted_vols) // 4]
            vol_75 = sorted_vols[3 * len(sorted_vols) // 4]
            
            if vol > vol_75:
                # High volatility: wider brackets
                vol_mult_stop = 1.15
                vol_mult_target = 1.60
            elif vol < vol_25:
                # Low volatility: tighter brackets
                vol_mult_stop = 0.95
                vol_mult_target = 1.40
            else:
                # Normal volatility: standard brackets
                vol_mult_stop = 1.08
                vol_mult_target = 1.52
        else:
            # Not enough history yet, use standard
            vol_mult_stop = 1.08
            vol_mult_target = 1.52
        
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
