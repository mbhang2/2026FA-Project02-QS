"""
Analysis of trials #51-#57 and breakthrough strategy:

Best score: #56 at +1.5407 with 683 trades.

Key observations from the progression:
- #51-#56 show that MA crossover + ATR volatility scaling is the core winning signal
- #57 added delta confirmation but only achieved +1.0152 (regression of -0.5255)
- This suggests: adding filters reduces edge, not improves it
- The base signal (MA crossover + bar strength + price tolerance) is already optimal

Critical insight: The score plateaus around +1.54 with ~683 trades. To break through,
we need to examine what's happening at the edges:

1. TRADE QUALITY vs QUANTITY: #56 has 683 trades at +1.5407. Are we missing high-quality
   entries because thresholds are too tight? Or are we taking too many mediocre trades?

2. VOLATILITY REGIME: Current vol_lookback=18 might miss fast volatility changes.
   Try vol_lookback=16 to be more responsive to recent volatility shifts.

3. ASYMMETRY IN STOP/TARGET: Currently vol_mult_stop=1.11, vol_mult_target=1.49.
   The ratio (1.49/1.11 = 1.34) might not be optimal. Try tighter stops (1.10) and
   higher targets (1.50) for better risk/reward ratio.

4. MA DIVERGENCE SENSITIVITY: Current min_ma_divergence=0.00140. This is very tight.
   Try 0.00138 to catch slightly earlier crossovers (more trades, hopefully quality ones).

5. PRICE TOLERANCE: Current 0.0021 works well. Keep it.

6. BAR STRENGTH: Current 0.30 is less restrictive than #56's 0.32. This might be catching
   too many weak bars. Try 0.31 as middle ground.

7. SIMPLICITY WINS: Don't add delta, don't add multi-timeframe, don't add momentum checks.
   The data shows adding filters consistently reduces score. Keep the core signal pure.

Strategy for #58: Micro-optimize the proven winning formula.
- vol_lookback: 16 (more responsive volatility)
- vol_multiplier_stop: 1.10 (tighter stops)
- vol_multiplier_target: 1.50 (higher targets)
- min_ma_divergence: 0.00138 (catch more crossovers)
- bar_strength_threshold: 0.31 (slightly more selective)
- Keep everything else from #56

Expected outcome: Break through +1.54 by optimizing the risk/reward ratio and
catching slightly more valid crossovers without adding complexity.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 16,
                 vol_multiplier_stop: float = 1.10,
                 vol_multiplier_target: float = 1.50,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00138,
                 bar_strength_threshold: float = 0.31,
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
            return
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        if bar_strength < self.bar_strength_threshold:
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
