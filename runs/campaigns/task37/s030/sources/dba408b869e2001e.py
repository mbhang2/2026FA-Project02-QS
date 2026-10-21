"""
Analysis of trials #0-#86 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. Trial #85-#86 explored order flow and adaptive sizing but didn't break through
3. The plateau at +1.54 is robust; all signal and sizing refinements hit diminishing returns
4. The core MA crossover (6/20) with volatility-scaled brackets is proven
5. What we haven't explored: ENTRY TIMING WITHIN THE SIGNAL - filtering by MOMENTUM DIRECTION

Critical insight - MOMENTUM CONFIRMATION WITH DIRECTIONAL STRENGTH:
The plateau suggests we need a fundamentally different entry filter. What if the issue
is that we're entering on MA crossover alone, but missing when the momentum is actually
strengthening vs. weakening?

New hypothesis - MOMENTUM ACCELERATION FILTER:
Instead of just MA crossover + order flow, add MOMENTUM ACCELERATION:

1. Keep the MA crossover framework (fast > slow for buys, fast < slow for sells)
2. Keep order flow confirmation (delta/volume ratio)
3. Add MOMENTUM ACCELERATION as a secondary confirmation:
   - Calculate momentum = current_price - price_N_bars_ago
   - Calculate momentum_acceleration = current_momentum - previous_momentum
   - Only trade if momentum is ACCELERATING in the trend direction
   - For buys: momentum_acceleration > 0 (momentum increasing)
   - For sells: momentum_acceleration < 0 (momentum decreasing)
4. This works because:
   - A crossover alone can be a false breakout
   - But a crossover WITH accelerating momentum is more likely to persist
   - It filters out whipsaws where momentum is already fading
5. Parameters:
   - momentum_lookback: 5 (measure momentum over 5 bars)
   - min_momentum_acceleration: 0.001 (require at least 0.1% acceleration)
   - Keep order_flow_threshold at 0.14
   - Keep vol scaling from #63

The insight: #63 worked well because it had clean signal + good parameters. But we
haven't tried filtering entries by whether momentum is ACCELERATING vs. DECELERATING.
A breakout with fading momentum is a trap; a breakout with accelerating momentum is
a trade. This should reduce false entries without reducing valid ones.

Expected outcome: Exceed #63's +1.5421 by filtering entries through momentum acceleration,
maintaining ~680 trades with better win rate and fewer whipsaws.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 momentum_lookback: int = 5,
                 vol_multiplier_stop: float = 1.10,
                 vol_multiplier_target: float = 1.50,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00140,
                 order_flow_threshold: float = 0.14,
                 min_momentum_acceleration: float = 0.001,
                 price_tolerance: float = 0.0021,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.momentum_lookback = momentum_lookback
        self.vol_multiplier_stop = vol_multiplier_stop
        self.vol_multiplier_target = vol_multiplier_target
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
        self.order_flow_threshold = order_flow_threshold
        self.min_momentum_acceleration = min_momentum_acceleration
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
        # Track momentum for acceleration calculation
        self.momentum_history: list[float] = []

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
        max_needed = self.slow_lookback + self.vol_lookback + self.momentum_lookback + 10
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
        
        # Calculate momentum (price change over momentum_lookback bars)
        momentum = 0.0
        if len(self.closes) >= self.momentum_lookback + 1:
            momentum = current_price - self.closes[-self.momentum_lookback - 1]
        
        self.momentum_history.append(momentum)
        if len(self.momentum_history) > self.momentum_lookback * 2:
            self.momentum_history = self.momentum_history[-(self.momentum_lookback * 2):]
        
        # Calculate momentum acceleration (change in momentum)
        momentum_acceleration = 0.0
        if len(self.momentum_history) >= 2:
            momentum_acceleration = self.momentum_history[-1] - self.momentum_history[-2]
        
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
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + positive order flow + accelerating momentum
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and order_flow_ratio > self.order_flow_threshold and
            momentum_acceleration > self.min_momentum_acceleration):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + negative order flow + decelerating momentum
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and order_flow_ratio < -self.order_flow_threshold and
              momentum_acceleration < -self.min_momentum_acceleration):
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
