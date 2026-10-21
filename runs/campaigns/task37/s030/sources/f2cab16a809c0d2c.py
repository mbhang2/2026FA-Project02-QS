"""
Analysis of trials #0-#85 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. Trial #85 used order flow (delta/volume) as entry confirmation
3. The plateau at +1.54 persists; order flow alone may not be the breakthrough
4. All attempts to add complexity have hit diminishing returns
5. The core MA crossover with volatility scaling is proven robust

Critical insight - ADAPTIVE POSITION SIZING WITH VOLATILITY:
The plateau suggests we've optimized entry signals to their limit. What if the
issue is not the SIGNAL but the POSITION SIZING?

New hypothesis - VOLATILITY-ADAPTIVE POSITION SIZING:
Instead of fixed size=1, scale the position size based on volatility regime:

1. Keep the proven MA crossover framework from #63 as the core filter
2. Keep order flow confirmation from #85 (it's a clean addition)
3. Add VOLATILITY-ADAPTIVE POSITION SIZING:
   - In high volatility: reduce size to 0.8x (reduce risk in choppy markets)
   - In normal volatility: use size 1.0x (baseline)
   - In low volatility: increase size to 1.2x (increase size in calm markets)
4. This naturally improves risk-adjusted returns because:
   - High vol markets are harder to predict; smaller size reduces drawdown
   - Low vol markets are more predictable; larger size captures better odds
   - It maintains similar trade frequency but improves win rate
5. Keep the order flow threshold at 0.15 but relax slightly to 0.14
6. Adjust min_ma_divergence from 0.00140 to 0.00145 (balance between signal strength and frequency)

The insight: #63 and #85 both scored ~1.54 with similar trade counts. The difference
between 1.54 and breakthrough (1.60+) might not be more entries, but BETTER entries
through volatility-aware sizing. High volatility periods should get smaller positions,
low volatility periods should get larger positions.

Also: The order flow threshold of 0.15 in #85 might be slightly too strict. Relaxing
to 0.14 allows more entries while still requiring meaningful order flow alignment.

Expected outcome: Exceed #63's +1.5421 by using volatility-adaptive sizing to improve
risk-adjusted returns, maintaining ~680 trades with better Sharpe ratio.
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
                 order_flow_threshold: float = 0.14,
                 price_tolerance: float = 0.0021,
                 base_size: int = 1,
                 vol_high_mult: float = 0.8,
                 vol_normal_mult: float = 1.0,
                 vol_low_mult: float = 1.2):
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
        self.order_flow_threshold = order_flow_threshold
        self.price_tolerance = price_tolerance
        self.base_size = base_size
        self.vol_high_mult = vol_high_mult
        self.vol_normal_mult = vol_normal_mult
        self.vol_low_mult = vol_low_mult
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
        
        # Determine volatility-adaptive position size
        adaptive_size = self.base_size
        if vol_regime > 0:
            vol_ratio = vol / vol_regime if vol_regime > 0 else 1.0
            # High volatility (vol_ratio > 1.3): reduce size
            if vol_ratio > 1.3:
                adaptive_size = max(1, int(self.base_size * self.vol_high_mult))
            # Low volatility (vol_ratio < 0.7): increase size
            elif vol_ratio < 0.7:
                adaptive_size = max(1, int(self.base_size * self.vol_low_mult))
            else:
                # Normal regime: use baseline size
                adaptive_size = max(1, int(self.base_size * self.vol_normal_mult))
        
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
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + positive order flow
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and order_flow_ratio > self.order_flow_threshold):
            ctx.buy(
                size=adaptive_size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + negative order flow
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and order_flow_ratio < -self.order_flow_threshold):
            ctx.sell(
                size=adaptive_size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
