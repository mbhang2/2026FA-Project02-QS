"""
Analysis of trials #0-#72 and strategic breakthrough:

Best score: #72 at +1.4682 with 684 trades.

Key observation: The top performers (#56, #63, #67, #70, #71) cluster around 
+1.54 with 680-700 trades. Trial #72 regressed to +1.4682, suggesting the 
dual-regime approach with strong_ma_divergence threshold was too restrictive.

Critical insight from the regression:
- The strong_ma_divergence=0.00350 threshold filtered out too many valid trades
- The choppy regime brackets (0.90, 1.35) were too tight
- We lost ~0.08 points and ~15 trades compared to #71

Root cause analysis:
- #71 achieved +1.4939 with 695 trades using percentile-based volatility scaling
- #72 tried to use MA divergence magnitude for regime selection, but this was 
  orthogonal to what actually works
- The winning formula is: entry selectivity (bar strength + MA divergence) + 
  volatility percentile-based exit sizing

New hypothesis - REFINED VOLATILITY PERCENTILE APPROACH:
Return to the #71 framework (which was proven at +1.4939) but with refinements:

1. Keep the proven entry signal (MA crossover + bar strength + price tolerance)
2. Keep volatility percentile-based regime detection (25th, 75th percentiles)
3. OPTIMIZATION: Adjust the bracket multipliers more precisely:
   - High vol (75th+): stop=1.18, target=1.62 (slightly wider than #71's 1.15/1.60)
   - Normal vol (25-75): stop=1.10, target=1.54 (tighter than #71's 1.08/1.52)
   - Low vol (<25th): stop=0.92, target=1.38 (tighter than #71's 0.95/1.40)
4. Increase bar_strength_threshold to 0.28 (from 0.25) for more selectivity
5. Increase min_ma_divergence to 0.00135 (from 0.00130) to filter marginal crosses
6. Reduce vol_history_depth to 80 (from 100) for faster adaptation to regime changes

The insight: #71 had the right architecture. We're not changing the framework,
just tuning the parameters more aggressively. The regression to #72 shows that
MA divergence magnitude is not the right axis for regime selection - volatility
percentile is.

Expected outcome: Score at or above +1.4939 (matching #71) with potential to 
exceed it through tighter entry selectivity (higher bar_strength and min_ma_divergence)
and optimized bracket multipliers.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_history_depth: int = 80,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00135,
                 bar_strength_threshold: float = 0.28,
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
        
        # Determine volatility regime using percentiles
        vol_mult_stop = 1.10
        vol_mult_target = 1.54
        
        if len(self.vol_history) >= 20:
            sorted_vols = sorted(self.vol_history)
            vol_25_idx = len(sorted_vols) // 4
            vol_75_idx = 3 * len(sorted_vols) // 4
            vol_25 = sorted_vols[vol_25_idx] if vol_25_idx < len(sorted_vols) else 0
            vol_75 = sorted_vols[vol_75_idx] if vol_75_idx < len(sorted_vols) else 0
            
            if vol > vol_75:
                # High volatility: wider brackets
                vol_mult_stop = 1.18
                vol_mult_target = 1.62
            elif vol < vol_25:
                # Low volatility: tighter brackets
                vol_mult_stop = 0.92
                vol_mult_target = 1.38
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
