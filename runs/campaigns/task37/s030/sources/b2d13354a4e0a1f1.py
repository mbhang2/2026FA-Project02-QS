"""
Analysis of trials #0-#67 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. The MA crossover + volatility scaling plateau is robust at +1.54
2. #67 tweaked #63's parameters slightly (bar_strength 0.30, price_tolerance 0.0020)
3. All attempts to add new signals or filters regress consistently
4. The core signal (fast MA > slow MA) is proven and stable

Critical insight from the data:
- #63-#67 all cluster around +1.54 with 680-690 trades
- Attempts to break out (add momentum, mean reversion, contraction) all failed
- The plateau suggests we're hitting the limit of THIS FEATURE SET

New hypothesis - ENTRY TIMING REFINEMENT:
What if the problem isn't the signal, but WHEN we enter relative to the MA?

Current logic in #67:
- BUY: fast > slow AND price >= (fast_ma - tolerance) AND bar closed up
- This enters when price is NEAR or ABOVE the fast MA

What if we're entering too late? What if we should enter when:
- BUY: fast > slow AND price is APPROACHING the fast MA from below
- This captures entries earlier in the trend, with better risk/reward

Specifically:
1. Keep the MA crossover signal (proven)
2. Keep volatility scaling (proven)
3. Change entry condition: Enter when price crosses THROUGH the fast MA
   - For longs: price was below fast_ma on previous bar, now at or above
   - For shorts: price was above fast_ma on previous bar, now at or below
4. This is a CROSSOVER entry, not a "price near MA" entry
5. Removes the bar_strength requirement (it was filtering too much)
6. Removes price_tolerance (it was adding complexity)

The insight: #67 had bar_strength_threshold=0.30 and price_tolerance=0.0020.
These are FILTERS that reduce trade count. What if we remove them and let the
fast MA crossover BE the entry signal? This should increase entries (more trades)
while maintaining quality through the trend filter (fast > slow).

Expected outcome: 700-750 trades with score at or above +1.5421 through better
entry timing and reduced over-filtering.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.10,
                 vol_multiplier_target: float = 1.50,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
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
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for volatility
        self.true_ranges: list[float] = []
        # Track previous close for true range calculation
        self.prev_close: float | None = None
        # Track previous fast MA for crossover detection
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
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Entry logic: MA crossover with fast MA crossing price
        # Buy: uptrend (fast > slow) AND price crosses above fast MA
        if fast_ma > slow_ma:
            # Check for upside crossover: price crosses above fast MA
            if self.prev_fast_ma is not None and current_price > fast_ma and self.prev_fast_ma <= current_price:
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-long",
                )
        # Sell: downtrend (fast < slow) AND price crosses below fast MA
        elif fast_ma < slow_ma:
            # Check for downside crossover: price crosses below fast MA
            if self.prev_fast_ma is not None and current_price < fast_ma and self.prev_fast_ma >= current_price:
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-short",
                )
        
        # Update previous fast MA for next iteration
        self.prev_fast_ma = fast_ma

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
