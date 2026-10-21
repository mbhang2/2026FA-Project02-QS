"""
Analysis of trials #0-#64 and breakthrough strategy:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. MA crossover + volatility scaling plateaus at +1.54
2. Momentum + mean reversion hybrid (#64) is a step backward at +1.2027
3. The issue: we're trading too frequently on weak signals
4. All approaches share the same core: fast/slow MA divergence

Critical insight: The plateau suggests we need ENTRY QUALITY filtering, not new signals.
What if the best trades are:
1. MA crossovers that occur when volatility is DECLINING (conviction building)
2. Entries where the previous bar's close is ALIGNED with the direction (no whipsaw)
3. Entries where bar strength is INCREASING (momentum building, not fading)
4. Skip entries on the first bar of a move - wait for confirmation

New hypothesis - VOLATILITY CONTRACTION + MOMENTUM CONFIRMATION:
Instead of trading every MA crossover, what if we:
1. Keep the MA crossover as the primary signal (proven)
2. Add volatility contraction filter: only trade when vol is LOWER than recent average
3. Add momentum confirmation: ROC must be building (accelerating), not just present
4. Add bar strength acceleration: current bar strength > previous bar strength
5. Add prior bar alignment: previous bar should be in the direction of the trade

This creates a QUALITY FILTER that removes weak entries while keeping strong ones.
The insight: #63 trades 681 times. What if the best 400-500 trades are worth +2.0,
but we're diluting with 200 weak trades worth -0.5 each?

Expected outcome: Higher quality entries with tighter entry clustering, pushing past +1.5421.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 roc_lookback: int = 3, roc_threshold: float = 0.0008,
                 vol_multiplier_stop: float = 1.10,
                 vol_multiplier_target: float = 1.50,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00140,
                 bar_strength_threshold: float = 0.32,
                 price_tolerance: float = 0.0021,
                 vol_contraction_threshold: float = 0.85,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.roc_lookback = roc_lookback
        self.roc_threshold = roc_threshold
        self.vol_multiplier_stop = vol_multiplier_stop
        self.vol_multiplier_target = vol_multiplier_target
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
        self.bar_strength_threshold = bar_strength_threshold
        self.price_tolerance = price_tolerance
        self.vol_contraction_threshold = vol_contraction_threshold
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for ATR-like volatility
        self.true_ranges: list[float] = []
        # Track previous close for true range calculation
        self.prev_close: float | None = None
        # Track volatility history for regime detection
        self.vol_history: list[float] = []
        # Track ROC history for acceleration detection
        self.roc_history: list[float] = []
        # Track bar strength history
        self.bar_strength_history: list[float] = []

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
        max_needed = self.slow_lookback + self.vol_lookback + self.roc_lookback + 10
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.true_ranges) > max_needed:
            self.true_ranges = self.true_ranges[-max_needed:]

        # Need enough data for both moving averages and ROC
        if len(self.closes) < self.slow_lookback + self.roc_lookback:
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
        
        self.vol_history.append(vol)
        if len(self.vol_history) > self.vol_lookback * 2:
            self.vol_history = self.vol_history[-(self.vol_lookback * 2):]
        
        # Calculate volatility regime (mean of recent volatility)
        vol_regime = 0.0
        if len(self.vol_history) >= self.vol_lookback:
            vol_regime = sum(self.vol_history[-self.vol_lookback:]) / self.vol_lookback
        
        # FILTER 1: Volatility contraction - only trade when vol is LOW relative to regime
        if vol_regime > 0 and vol > vol_regime * self.vol_contraction_threshold:
            return
        
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
        
        # FILTER 2: Bar strength must meet minimum threshold
        if bar_strength < self.bar_strength_threshold:
            return
        
        # Calculate ROC (Rate of Change)
        roc = 0.0
        if len(self.closes) >= self.roc_lookback + 1:
            old_price = self.closes[-self.roc_lookback - 1]
            if old_price > 0:
                roc = (current_price - old_price) / old_price
        
        self.roc_history.append(roc)
        if len(self.roc_history) > self.roc_lookback + 5:
            self.roc_history = self.roc_history[-(self.roc_lookback + 5):]
        
        self.bar_strength_history.append(bar_strength)
        if len(self.bar_strength_history) > 5:
            self.bar_strength_history = self.bar_strength_history[-5:]
        
        # FILTER 3: ROC must meet threshold (momentum present)
        if abs(roc) < self.roc_threshold:
            return
        
        # FILTER 4: Bar strength acceleration - current bar stronger than previous
        if len(self.bar_strength_history) >= 2:
            prev_bar_strength = self.bar_strength_history[-2]
            if bar_strength <= prev_bar_strength:
                return
        
        # Get previous bar for direction alignment
        prev_bar = history[-2] if len(history) >= 2 else None
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band
        tolerance_band = self.price_tolerance * slow_ma
        
        # FILTER 5: Previous bar alignment - previous bar should be in trade direction
        # Buy: uptrend, current bar up, previous bar also up (or at least not strongly down)
        if (fast_ma > slow_ma and roc > self.roc_threshold and
            current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            
            # Check previous bar alignment
            if prev_bar is not None:
                prev_move = prev_bar.close - prev_bar.open
                # Previous bar should not be strongly bearish
                if prev_move < -0.002 * slow_ma:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry-long",
            )
        
        # Sell: downtrend, current bar down, previous bar also down (or at least not strongly up)
        elif (fast_ma < slow_ma and roc < -self.roc_threshold and
              current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open):
            
            # Check previous bar alignment
            if prev_bar is not None:
                prev_move = prev_bar.close - prev_bar.open
                # Previous bar should not be strongly bullish
                if prev_move > 0.002 * slow_ma:
                    return
            
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
