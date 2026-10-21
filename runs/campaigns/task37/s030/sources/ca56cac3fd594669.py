"""
Analysis of trials #0-#65 and strategic refinement:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. MA crossover + volatility scaling plateaus at +1.54
2. Adding complexity (momentum, mean reversion, volatility contraction) consistently regresses
3. #65 was over-filtered and exposed in only 23% of sessions (rejected)
4. The core signal (fast MA > slow MA) is proven, but we're trading too selectively or too broadly

Critical insight: The rejection of #65 for underexposure reveals the problem. We need:
- CONSISTENT ENTRY GENERATION (not too selective)
- QUALITY FILTERING (not adding new signals, but refining the existing one)
- SIMPLICITY (the best results come from the simplest approaches)

The pattern: #56-#63 all score around +1.54 with 640-680 trades. They're stable.
#64-#65 tried to be clever and failed. #63 is the sweet spot.

New hypothesis - REFINED SIMPLICITY:
Instead of adding filters, what if we:
1. Keep the MA crossover as the primary and ONLY signal
2. Simplify the volatility scaling (remove adaptive bar strength complexity)
3. Use ONLY price position relative to MAs (no bar strength, no ROC, no contraction)
4. Trade every clear MA crossover with consistent position sizing
5. Let the bracket handle all exits (stop_offset, target_offset)

The insight: #63 had 681 trades and scored +1.5421. That's roughly +0.00226 per trade.
What if we're overthinking it? What if the best approach is:
- BUY when: fast_ma > slow_ma AND price > fast_ma (uptrend, price above fast)
- SELL when: fast_ma < slow_ma AND price < fast_ma (downtrend, price below fast)
- Use volatility-scaled brackets (proven in #56-#63)
- Remove ALL other filters

This is a regression to core principles. #63 succeeded because it was simple enough to be robust.

Expected outcome: Consistent ~680 trades, score at or above +1.5421 through simplification
rather than complication.
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
        
        # Simple, core signal: MA crossover + price position
        # Buy: uptrend (fast > slow) AND price above fast MA
        if fast_ma > slow_ma and current_price > fast_ma:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) AND price below fast MA
        elif fast_ma < slow_ma and current_price < fast_ma:
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
