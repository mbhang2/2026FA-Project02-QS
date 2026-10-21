"""
Analysis of trials #0-#84 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. Trial #84 refined parameters but score regressed to +0.6450
3. The plateau at +1.54 is robust; incremental tuning of the same signal hits diminishing returns
4. Trials #82-#83 (dual signals) regressed badly, showing signal interference
5. The original momentum strategy (#0-#3) showed mean reversion can work differently

Critical insight - ENTRY TIMING OPTIMIZATION WITH ORDER FLOW:
The plateau suggests we've optimized bar-level features to their limit. What if the
issue is not the SIGNAL but the ENTRY TIMING within the bar?

New hypothesis - VOLUME-WEIGHTED ENTRY CONFIRMATION:
Instead of just bar_strength (body/range), use ORDER FLOW SIGNAL (delta/volume):

1. Keep the proven MA crossover framework as the core filter
2. Replace bar_strength with ORDER FLOW CONFIRMATION:
   - delta = buy_volume - sell_volume
   - order_flow_ratio = delta / total_volume (signed imbalance)
   - Only trade if order_flow_ratio aligns with trend direction
   - High positive ratio (>0.15) for buys, low negative ratio (<-0.15) for sells
3. This is more robust than bar_strength because:
   - It captures actual market participant behavior (order flow)
   - It's harder to fake than a bar's open/close positions
   - It naturally adapts to different volatility regimes
4. Keep volatility-scaled stops/targets from #63
5. Adjust parameters:
   - min_ma_divergence: 0.00155 → 0.00140 (relax slightly since we have better entry filter)
   - order_flow_threshold: 0.15 (require 15% signed imbalance)
   - vol_multiplier_stop: 1.10 (back to #63 values)
   - vol_multiplier_target: 1.50

The insight: #63 worked because it had clean signal + good parameters. But it only
looked at bar structure (open/close/range). Order flow (delta/volume) is a different
dimension that captures market microstructure. Combining MA crossover with order flow
should improve entry quality without the interference we saw with dual signals.

Expected outcome: Exceed #63's +1.5421 by using order flow as entry confirmation
instead of bar structure, maintaining ~680 trades with better win rate.
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
                 order_flow_threshold: float = 0.15,
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
        self.order_flow_threshold = order_flow_threshold
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
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + negative order flow
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and order_flow_ratio < -self.order_flow_threshold):
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
