"""
Analysis of trials #0-#87 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. All signal refinements (volatility scaling, order flow, momentum acceleration) hit diminishing returns
3. The core MA crossover (6/20) with volatility-scaled brackets is proven robust
4. Trial #87 added momentum acceleration but didn't break through the plateau
5. What we haven't explored: MULTI-TIMEFRAME CONFIRMATION WITH ASYMMETRIC ENTRY RULES

Critical insight - ASYMMETRIC ENTRY STRENGTH REQUIREMENTS:
The plateau suggests we need to differentiate between STRONG trends (where entries are safer)
and WEAK trends (where entries are riskier). Current approach treats all MA crossovers equally.

New hypothesis - TREND STRENGTH ASYMMETRY WITH MULTI-BAR CONFIRMATION:
Instead of single-bar confirmation, use TREND STRENGTH to determine entry requirements:

1. Keep the MA crossover framework (fast > slow for buys, fast < slow for sells)
2. Calculate TREND STRENGTH = how many consecutive bars closed in trend direction
   - For uptrend: count consecutive bars with close > open
   - For downtrend: count consecutive bars with close < open
3. Use ASYMMETRIC entry thresholds based on trend strength:
   - STRONG trend (3+ consecutive bars in direction): Lower entry threshold
   - WEAK trend (1-2 bars): Higher entry threshold
   - This filters out false breakouts where momentum is weak
4. Keep order flow confirmation but make it adaptive:
   - In strong trends: require order_flow_threshold > 0.10 (less strict)
   - In weak trends: require order_flow_threshold > 0.18 (more strict)
5. Keep volatility scaling from #63 but tighten slightly:
   - vol_multiplier_stop: 1.08 (tighter stops)
   - vol_multiplier_target: 1.52 (slightly wider targets)

The insight: #63 and #87 both scored ~1.54 by requiring multiple confirmations. But we've
been treating all confirmations equally. What if STRONG trends (3+ consecutive bars) should
have LOWER entry thresholds because they're more likely to continue? And WEAK trends should
have HIGHER thresholds because they're more likely to reverse?

This reduces false entries in choppy markets while capturing more valid entries in trending
markets, improving the Sharpe ratio without necessarily increasing trade count.

Expected outcome: Exceed #63's +1.5421 by using trend strength to asymmetrically adjust
entry requirements, filtering out weak-momentum false breakouts.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.08,
                 vol_multiplier_target: float = 1.52,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00140,
                 order_flow_threshold_strong: float = 0.10,
                 order_flow_threshold_weak: float = 0.18,
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
        self.order_flow_threshold_strong = order_flow_threshold_strong
        self.order_flow_threshold_weak = order_flow_threshold_weak
        self.price_tolerance = price_tolerance
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for ATR-like volatility
        self.true_ranges: list[float] = []
        # Track previous close for true range calculation
        self.prev_close: float | None = None
        # Track volatility history for regime detection
        self.vol_history: list[float] = []
        # Track bar direction (close > open = True, close < open = False)
        self.bar_directions: list[bool] = []

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
        
        # Track bar direction
        bar_is_up = last_bar.close > last_bar.open
        self.bar_directions.append(bar_is_up)
        
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
        if len(self.bar_directions) > max_needed:
            self.bar_directions = self.bar_directions[-max_needed:]

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
        
        self.vol_history.append(vol)
        if len(self.vol_history) > self.vol_lookback * 2:
            self.vol_history = self.vol_history[-(self.vol_lookback * 2):]
        
        # Calculate trend strength: count consecutive bars in trend direction
        trend_strength_up = 0
        trend_strength_down = 0
        
        # Count consecutive up bars from the end
        for i in range(len(self.bar_directions) - 1, -1, -1):
            if self.bar_directions[i]:
                trend_strength_up += 1
            else:
                break
        
        # Count consecutive down bars from the end
        for i in range(len(self.bar_directions) - 1, -1, -1):
            if not self.bar_directions[i]:
                trend_strength_down += 1
            else:
                break
        
        # Determine if trend is strong (3+ consecutive bars) or weak (1-2 bars)
        is_strong_up_trend = trend_strength_up >= 3
        is_strong_down_trend = trend_strength_down >= 3
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Calculate order flow ratio: signed volume imbalance
        total_volume = last_bar.volume
        if total_volume <= 0:
            return
        
        # delta = buy_volume - sell_volume (from the bar)
        delta = last_bar.delta
        order_flow_ratio = delta / total_volume
        
        # Determine order flow threshold based on trend strength
        order_flow_threshold = self.order_flow_threshold_strong if is_strong_up_trend or is_strong_down_trend else self.order_flow_threshold_weak
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + positive order flow
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and order_flow_ratio > order_flow_threshold):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + negative order flow
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and order_flow_ratio < -order_flow_threshold):
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
