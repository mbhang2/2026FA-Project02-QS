"""
Analysis of trials #0-#81 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers (#56, #63, #67, #70, #71, #80, #81) cluster around +1.54 with 680-700 trades
2. The core MA crossover + volatility scaling + bar strength + momentum signal is proven
3. Trial #81 introduced volatility-adaptive momentum thresholds
4. Current best achieves 681 trades = 6.5 trades/session on average
5. All recent improvements have been incremental; the plateau suggests we need a different angle

Critical insight - ENTRY TIMING WITH PRICE ACTION CONFIRMATION:
The plateau at +1.54 suggests the current signal needs a secondary confirmation that
doesn't reduce trade frequency but improves entry quality through price action.

New hypothesis - PRICE ACTION CONFIRMATION WITH VOLATILITY CONTEXT:
Instead of just momentum, add a PRICE ACTION CONFIRMATION that looks at recent bars:

1. Keep the proven entry framework from #81
2. Add a price action filter: Check if the last 2 bars show directional alignment
   - For buys: Both of last 2 bars should close above their open (upside bars)
   - For sells: Both of last 2 bars should close below their open (downside bars)
   - This ensures we're in a genuine momentum sequence, not just one strong bar
3. Adjust bar_strength_threshold from 0.32 to 0.30 (allow slightly more entries)
4. Fine-tune price_tolerance from 0.0020 to 0.0018 (tighter MA band)
5. Adjust momentum_threshold_normal from 0.15 to 0.14 (slightly more permissive)
6. Keep vol_multiplier_stop at 1.10 and target at 1.50 (proven levels)
7. Adjust min_ma_divergence from 0.00140 to 0.00142 (slightly tighter MA requirement)

The insight: Price action confirmation (consecutive directional bars) adds a secondary
signal that filters for genuine momentum without being overly restrictive. This should
improve win rate while maintaining trade frequency for exposure floor compliance.

Expected outcome: Exceed #63's +1.5421 with price action confirmation that ensures
entries occur in genuine momentum sequences, not just single strong bars.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.10,
                 vol_multiplier_target: float = 1.50,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00142,
                 bar_strength_threshold: float = 0.30,
                 bar_strength_low_vol: float = 0.25,
                 bar_strength_high_vol: float = 0.40,
                 price_tolerance: float = 0.0018,
                 momentum_threshold_normal: float = 0.14,
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
        
        if not price_action_confirmed:
            return
        
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
