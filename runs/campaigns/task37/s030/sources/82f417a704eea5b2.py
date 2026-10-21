"""
Analysis of trials #0-#80 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers (#56, #63, #67, #70, #71) cluster around +1.54 with 680-700 trades
2. Trial #80 added momentum confirmation to #79's parameters
3. The core MA crossover + volatility scaling + bar strength signal is proven
4. Current best achieves 681 trades = 6.5 trades/session on average
5. Momentum threshold in #80 (0.15 / -0.15) may be too restrictive

Critical insight - ADAPTIVE MOMENTUM THRESHOLDS:
The plateau at +1.54 suggests we need better entry selectivity WITHOUT reducing
trade frequency. Trial #80's fixed momentum thresholds might be too strict.

New hypothesis - VOLATILITY-ADAPTIVE MOMENTUM THRESHOLDS:
Instead of fixed momentum thresholds, adapt them based on volatility regime:

1. Keep the proven entry framework from #63/#80
2. Make momentum thresholds ADAPTIVE to volatility:
   - High volatility: Lower momentum threshold (0.08 for buys, -0.08 for sells)
   - Low volatility: Higher momentum threshold (0.22 for buys, -0.22 for sells)
   - Normal volatility: Medium threshold (0.15 for buys, -0.15 for sells)
3. This ensures we're more selective when volatility is low (cleaner signals)
   and more aggressive when volatility is high (fewer false breakouts)
4. Slightly adjust bar_strength_threshold from 0.31 to 0.32 (sweet spot)
5. Fine-tune vol_multiplier_stop from 1.09 to 1.10, target from 1.51 to 1.50
6. Adjust min_ma_divergence from 0.00138 to 0.00140 (back to proven level)

The insight: Adaptive momentum thresholds align entry selectivity with market regime.
High volatility = more noise = relax momentum requirement. Low volatility = cleaner
moves = require higher momentum conviction. This should improve win rate while
maintaining trade frequency for exposure floor compliance.

Expected outcome: Exceed #63's +1.5421 with volatility-adaptive momentum that
provides better entry quality while maintaining 6+ trades per session.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.10,
                 vol_multiplier_target: float = 1.50,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00140,
                 bar_strength_threshold: float = 0.32,
                 bar_strength_low_vol: float = 0.25,
                 bar_strength_high_vol: float = 0.40,
                 price_tolerance: float = 0.0020,
                 momentum_threshold_normal: float = 0.15,
                 momentum_threshold_high_vol: float = 0.08,
                 momentum_threshold_low_vol: float = 0.22,
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
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Calculate bar strength (directional conviction)
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        bar_body = abs(last_bar.close - last_bar.open)
        bar_strength = bar_body / bar_range
        
        if bar_strength < adaptive_bar_strength:
            return
        
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
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
        # + momentum confirmation (adaptive)
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and momentum > adaptive_momentum_threshold):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
        # + momentum confirmation (adaptive)
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and momentum < -adaptive_momentum_threshold):
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
