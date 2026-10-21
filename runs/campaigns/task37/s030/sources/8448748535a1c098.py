"""
Analysis of trials #0-#61 and strategic breakthrough:

Best score: #56 at +1.5407 with 683 trades.

Key observations from the full progression:
1. Core signal (MA crossover + ATR volatility scaling) is proven and stable
2. All attempts to add filters or complexity reduce score
3. Micro-parameter tuning shows we're at a local optimum around +1.54
4. The mean reversion complement in #61 regressed significantly (0.6219)
5. Adaptive volatility-based selectivity in #61 also hurt performance

Critical insight: The plateau at +1.54 suggests we've optimized the current
approach to its natural limit. The regression in #61 (from +1.5407 to +0.6219)
shows that adding complexity, even with good intentions, breaks what works.

The winning formula from #56:
- Fast MA (6-bar) vs Slow MA (20-bar) crossover
- ATR-based volatility scaling (vol_lookback=18)
- Bar strength confirmation (0.32 threshold)
- Price tolerance band (0.0021) for whipsaw reduction
- Asymmetric vol multipliers: stop=1.12, target=1.48
- Simple, clean, proven

To break through +1.5407, we need to reconsider the ENTRY TIMING and POSITION
SIZING strategy, not add more signals or filters.

New hypothesis - DUAL-TIMEFRAME CONFIRMATION:
Instead of trading on a single 5m bar, what if we:
1. Keep the 5m MA crossover as the primary signal (proven at +1.54)
2. Add a 1m confirmation: only enter if the 1m bar also confirms the direction
3. This filters out false crossovers caught on weak 1m bars
4. Expected: Higher quality entries, fewer whipsaws, better score

Alternative hypothesis - ENTRY SCALING:
What if position size should scale with volatility regime?
1. High volatility: smaller size (risk management)
2. Low volatility: larger size (better capture)
3. Keep the same signal, but adapt size to market conditions

Testing the simpler hypothesis first: dual-timeframe confirmation with 1m bars.
This adds no new parameters, just uses existing data more intelligently.
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
        # For 1m confirmation
        self.closes_1m: list[float] = []
        self.prev_close_1m: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe("1m")  # Add 1m bars for confirmation

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
        
        # Check 1m confirmation - only enter if 1m bar also confirms direction
        history_1m = ctx.history("1m")
        if len(history_1m) < 1:
            return
        
        last_bar_1m = history_1m[-1]
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up + 1m confirms
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and last_bar_1m.close > last_bar_1m.open):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down + 1m confirms
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and last_bar_1m.close < last_bar_1m.open):
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
