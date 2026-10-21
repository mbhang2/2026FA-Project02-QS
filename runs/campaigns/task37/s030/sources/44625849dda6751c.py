"""
Analysis of trials #0-#71 and breakthrough strategy:

Best score: #63 at +1.5421 with 681 trades.

Key observation: The top performers (#56, #63, #67, #70, #71) all cluster 
tightly around +1.54 with 680-700 trades. The MA crossover + volatility scaling 
core is proven and stable.

Critical insight from plateau analysis:
- Entry signal is well-optimized (consistent 680+ trades)
- Exit timing is the remaining frontier
- Current approach: fixed volatility-scaled offsets
- The bracket is armed at fill and never adjusted

New hypothesis - DUAL-REGIME EXIT MANAGEMENT:
Instead of one-size-fits-all volatility scaling, what if we:

1. Keep the proven entry signal (MA crossover + bar strength + price tolerance)
2. Implement TWO separate volatility regimes with DIFFERENT bracket strategies:
   - TRENDING REGIME (strong MA divergence): Wider, more generous brackets
     (let winners run, accept wider stops)
   - CHOPPY REGIME (weak MA divergence): Tighter, more protective brackets
     (lock in gains faster, tighter stops)
3. Use the MA divergence magnitude itself to determine regime, not just direction
4. Scale the multipliers more aggressively between regimes

The insight: We've been treating all trades the same. But a trade entering on
a strong 0.005 MA divergence is fundamentally different from one on a weak 0.0013
divergence. The strong one should run wider; the weak one should exit tighter.

Also: The bar_strength_threshold of 0.25 might be slightly too loose. Try 0.27
to be more selective on marginal setups without being overly restrictive.

Expected outcome: Score at or above +1.5421 through intelligent regime-aware
exit sizing that adapts bracket width to signal strength.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00130,
                 strong_ma_divergence: float = 0.00350,
                 bar_strength_threshold: float = 0.27,
                 price_tolerance: float = 0.0020,
                 trending_vol_mult_stop: float = 1.15,
                 trending_vol_mult_target: float = 1.65,
                 choppy_vol_mult_stop: float = 0.90,
                 choppy_vol_mult_target: float = 1.35,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
        self.strong_ma_divergence = strong_ma_divergence
        self.bar_strength_threshold = bar_strength_threshold
        self.price_tolerance = price_tolerance
        self.trending_vol_mult_stop = trending_vol_mult_stop
        self.trending_vol_mult_target = trending_vol_mult_target
        self.choppy_vol_mult_stop = choppy_vol_mult_stop
        self.choppy_vol_mult_target = choppy_vol_mult_target
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for volatility
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
        
        # Determine regime based on MA divergence strength
        if ma_divergence >= self.strong_ma_divergence:
            # TRENDING REGIME: Strong signal, wider brackets
            vol_mult_stop = self.trending_vol_mult_stop
            vol_mult_target = self.trending_vol_mult_target
        else:
            # CHOPPY REGIME: Weak signal, tighter brackets
            vol_mult_stop = self.choppy_vol_mult_stop
            vol_mult_target = self.choppy_vol_mult_target
        
        # Use regime-aware volatility scaling for stop and target
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
