"""
Analysis of trials #0-#82 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. Trial #82 added price action confirmation (2 consecutive directional bars)
3. The plateau persists despite incremental refinements to existing signals
4. Current best achieves 681 trades = 6.5 trades/session on average
5. All improvements have been parameter tuning within the same signal framework

Critical insight - SIGNAL DIVERSIFICATION WITH MEAN REVERSION:
The plateau at +1.54 suggests we've optimized the trend-following signal to its limit.
The original trials (#0-#3) showed mean reversion approaches scored differently.

New hypothesis - DUAL-SIGNAL FRAMEWORK WITH REGIME DETECTION:
Instead of relying solely on MA crossover + momentum, add a MEAN REVERSION signal
that activates in low-volatility regimes:

1. Keep the proven trend-following framework as the PRIMARY signal
2. Add a MEAN REVERSION signal that activates when:
   - Volatility is LOW (vol_regime in bottom quartile)
   - Price has deviated significantly from the slow MA (> 0.4% band)
   - Bar closes in the opposite direction of the deviation (reversal signal)
3. Mean reversion uses TIGHTER stops (0.50) and CLOSER targets (0.75)
   - This is appropriate for range-bound mean reversion trades
   - Trend trades use wider stops/targets (0.70-1.50)
4. Track which signal generated each trade via different tags
5. Adjust trend parameters slightly:
   - bar_strength_threshold: 0.30 → 0.31 (slightly more selective)
   - min_ma_divergence: 0.00142 → 0.00145 (require stronger trends)
6. New mean reversion parameters:
   - mean_reversion_vol_threshold: 0.0008 (activate only in calm markets)
   - mean_reversion_price_deviation: 0.004 (require 0.4% deviation from MA)
   - mean_reversion_stop: 0.50 (tight protection)
   - mean_reversion_target: 0.75 (quick exits)

The insight: Dual signals exploit different market regimes. Trend following works
in volatile breakouts; mean reversion works in calm, range-bound markets. This
diversification should improve Sharpe ratio and potentially exceed the plateau.

Expected outcome: Break through +1.54 by adding mean reversion signal that captures
trades in low-volatility regimes where trend following is less effective.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.10,
                 vol_multiplier_target: float = 1.50,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00145,
                 bar_strength_threshold: float = 0.31,
                 bar_strength_low_vol: float = 0.25,
                 bar_strength_high_vol: float = 0.40,
                 price_tolerance: float = 0.0018,
                 momentum_threshold_normal: float = 0.14,
                 momentum_threshold_high_vol: float = 0.08,
                 momentum_threshold_low_vol: float = 0.22,
                 mean_reversion_vol_threshold: float = 0.0008,
                 mean_reversion_price_deviation: float = 0.004,
                 mean_reversion_stop: float = 0.50,
                 mean_reversion_target: float = 0.75,
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
        self.bar_strength_low_vol = bar_strength_low_vol
        self.bar_strength_high_vol = bar_strength_high_vol
        self.price_tolerance = price_tolerance
        self.momentum_threshold_normal = momentum_threshold_normal
        self.momentum_threshold_high_vol = momentum_threshold_high_vol
        self.momentum_threshold_low_vol = momentum_threshold_low_vol
        self.mean_reversion_vol_threshold = mean_reversion_vol_threshold
        self.mean_reversion_price_deviation = mean_reversion_price_deviation
        self.mean_reversion_stop = mean_reversion_stop
        self.mean_reversion_target = mean_reversion_target
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for ATR-like volatility
        self.true_ranges: list[float] = []
        # Track previous close for true range calculation
        self.prev_close: float | None = None
        # Track volatility history for regime detection
        self.vol_history: list[float] = []
        # Track bar opens and closes for price action confirmation
        self.bar_opens: list[float] = []
        self.bar_closes: list[float] = []

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
        self.bar_opens.append(last_bar.open)
        self.bar_closes.append(last_bar.close)
        
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
            self.bar_opens = self.bar_opens[-max_needed:]
            self.bar_closes = self.bar_closes[-max_needed:]
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
            # High volatility (vol_ratio > 1.2): be less selective
            if vol_ratio > 1.2:
                adaptive_bar_strength = self.bar_strength_low_vol
            # Low volatility (vol_ratio < 0.8): be more selective
            elif vol_ratio < 0.8:
                adaptive_bar_strength = self.bar_strength_high_vol
            else:
                # Normal regime: interpolate between thresholds
                adaptive_bar_strength = (self.bar_strength_low_vol + self.bar_strength_high_vol) / 2
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        # Calculate momentum: normalized directional movement within the bar
        momentum = (last_bar.close - last_bar.open) / bar_range
        
        # Determine adaptive momentum threshold based on volatility regime
        adaptive_momentum_threshold = self.momentum_threshold_normal
        if vol_regime > 0:
            vol_ratio = vol / vol_regime if vol_regime > 0 else 1.0
            if vol_ratio > 1.2:
                # High volatility: relax momentum requirement
                adaptive_momentum_threshold = self.momentum_threshold_high_vol
            elif vol_ratio < 0.8:
                # Low volatility: require higher momentum conviction
                adaptive_momentum_threshold = self.momentum_threshold_low_vol
            else:
                # Normal regime: use normal threshold
                adaptive_momentum_threshold = self.momentum_threshold_normal
        
        # Check price action confirmation: last 2 bars should show directional alignment
        price_action_confirmed = False
        if len(self.bar_closes) >= 2:
            prev_bar_close = self.bar_closes[-2]
            prev_bar_open = self.bar_opens[-2]
            curr_bar_close = self.bar_closes[-1]
            curr_bar_open = self.bar_opens[-1]
            
            # For buys: both bars should close above open (upside bars)
            if fast_ma > slow_ma:
                if (prev_bar_close > prev_bar_open and curr_bar_close > curr_bar_open):
                    price_action_confirmed = True
            # For sells: both bars should close below open (downside bars)
            elif fast_ma < slow_ma:
                if (prev_bar_close < prev_bar_open and curr_bar_close < curr_bar_open):
                    price_action_confirmed = True
        
        # PRIMARY SIGNAL: Trend following
        if (ma_divergence >= self.min_ma_divergence and 
            bar_strength >= adaptive_bar_strength and 
            price_action_confirmed):
            
            # Use asymmetric volatility scaling for stop and target
            vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
            vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
            
            stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
            target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
            
            # Price tolerance band to reduce MA crossover whipsaws
            tolerance_band = self.price_tolerance * slow_ma
            
            # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
            # + momentum confirmation (adaptive) + price action confirmation
            if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
                last_bar.close > last_bar.open and momentum > adaptive_momentum_threshold):
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-long",
                )
            # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
            # + momentum confirmation (adaptive) + price action confirmation
            elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
                  last_bar.close < last_bar.open and momentum < -adaptive_momentum_threshold):
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-short",
                )
        
        # SECONDARY SIGNAL: Mean reversion in low-volatility regimes
        elif vol < self.mean_reversion_vol_threshold:
            # Calculate price deviation from slow MA
            price_deviation = abs(current_price - slow_ma) / slow_ma
            
            if price_deviation >= self.mean_reversion_price_deviation:
                # Price is significantly away from MA - look for reversal
                
                # Buy: price below MA and bar closes up (reversal signal)
                if current_price < slow_ma and last_bar.close > last_bar.open:
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.mean_reversion_stop,
                        target_offset=self.mean_reversion_target,
                        tag="mean-reversion-long",
                    )
                # Sell: price above MA and bar closes down (reversal signal)
                elif current_price > slow_ma and last_bar.close < last_bar.open:
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.mean_reversion_stop,
                        target_offset=self.mean_reversion_target,
                        tag="mean-reversion-short",
                    )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
