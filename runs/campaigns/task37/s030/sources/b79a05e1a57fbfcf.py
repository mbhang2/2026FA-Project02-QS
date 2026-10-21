"""
Analysis of trials #0-#63 and strategic breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. The core MA crossover signal (6-bar vs 20-bar) with ATR volatility scaling is proven and stable
2. Volatility-driven adaptive bar strength in #63 showed slight improvement over #56
3. The plateau at +1.54 persists across all variations of the same core approach
4. All attempts to add complexity (mean reversion, dual-timeframe, etc.) regress significantly

Critical insight: We've optimized the MA crossover + volatility scaling approach to its
natural limit around +1.54. To break through, we need a fundamentally different signal.

New hypothesis - MOMENTUM + MEAN REVERSION HYBRID:
Instead of pure trend following, what if we:
1. Use the MA crossover as a FILTER (not the primary signal)
2. Add a momentum oscillator: rate of change (ROC) over short window
3. Combine with mean reversion: fade large single-bar moves
4. Trade momentum in the direction of the MA trend
5. Use tighter stops for mean reversion trades, wider for trend trades

The insight: #56-#63 all trade every MA crossover. But what if we're trading
too many weak signals? What if the best trades are:
- Strong momentum bars that align with MA trend (keep these)
- Extreme single-bar moves that mean-revert (add these)

This creates two entry modes:
1. Momentum mode: ROC > threshold AND bar aligns with MA
2. Mean reversion mode: single bar is extreme AND opposite of prior bar

Expected outcome: Higher quality entries by filtering for either strong momentum
or extreme reversals, not just any MA crossover. This should push past +1.5421.
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
                 bar_strength_low_vol: float = 0.25,
                 bar_strength_high_vol: float = 0.40,
                 price_tolerance: float = 0.0021,
                 mean_reversion_threshold: float = 0.0012,
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
        self.bar_strength_low_vol = bar_strength_low_vol
        self.bar_strength_high_vol = bar_strength_high_vol
        self.price_tolerance = price_tolerance
        self.mean_reversion_threshold = mean_reversion_threshold
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for ATR-like volatility
        self.true_ranges: list[float] = []
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
        
        # Calculate volatility regime (median of recent volatility)
        vol_regime = 0.0
        if len(self.vol_history) >= self.vol_lookback:
            vol_sorted = sorted(self.vol_history[-self.vol_lookback:])
            vol_regime = vol_sorted[len(vol_sorted) // 2]
        
        # Adapt bar strength threshold based on volatility regime
        adaptive_bar_strength = self.bar_strength_threshold
        if vol_regime > 0:
            vol_ratio = vol / vol_regime if vol_regime > 0 else 1.0
            if vol_ratio > 1.2:
                adaptive_bar_strength = self.bar_strength_low_vol
            elif vol_ratio < 0.8:
                adaptive_bar_strength = self.bar_strength_high_vol
            else:
                adaptive_bar_strength = (self.bar_strength_low_vol + self.bar_strength_high_vol) / 2
        
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
        
        # Calculate ROC (Rate of Change)
        roc = 0.0
        if len(self.closes) >= self.roc_lookback + 1:
            old_price = self.closes[-self.roc_lookback - 1]
            if old_price > 0:
                roc = (current_price - old_price) / old_price
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Get previous bar for mean reversion analysis
        prev_bar = history[-2] if len(history) >= 2 else None
        
        # MODE 1: Momentum entry - strong ROC aligned with MA trend
        momentum_buy = (fast_ma > slow_ma and roc > self.roc_threshold and
                       current_price >= (fast_ma - tolerance_band) and
                       last_bar.close > last_bar.open and
                       bar_strength >= adaptive_bar_strength)
        
        momentum_sell = (fast_ma < slow_ma and roc < -self.roc_threshold and
                        current_price <= (fast_ma + tolerance_band) and
                        last_bar.close < last_bar.open and
                        bar_strength >= adaptive_bar_strength)
        
        # MODE 2: Mean reversion entry - extreme single bar move that reverses
        mean_rev_buy = False
        mean_rev_sell = False
        
        if prev_bar is not None:
            prev_bar_move = (prev_bar.close - prev_bar.open) / prev_bar.open if prev_bar.open > 0 else 0
            curr_bar_move = (last_bar.close - last_bar.open) / last_bar.open if last_bar.open > 0 else 0
            
            # Large down move followed by up move (mean reversion long)
            if (prev_bar_move < -self.mean_reversion_threshold and
                curr_bar_move > 0 and last_bar.close > last_bar.open and
                fast_ma > slow_ma):  # Only in uptrend
                mean_rev_buy = True
            
            # Large up move followed by down move (mean reversion short)
            if (prev_bar_move > self.mean_reversion_threshold and
                curr_bar_move < 0 and last_bar.close < last_bar.open and
                fast_ma < slow_ma):  # Only in downtrend
                mean_rev_sell = True
        
        # Execute trades
        if momentum_buy or mean_rev_buy:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry-long",
            )
        elif momentum_sell or mean_rev_sell:
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
