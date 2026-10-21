"""
Analysis of trials #48-#51 and the path to breakthrough:

Best score: #51 at +1.5208 with 682 trades (micro-adjustments to #48).

Key observations:
1. #48: +1.4177 (660 trades) - ATR-based volatility baseline
2. #51: +1.5208 (682 trades) - BREAKTHROUGH with micro-adjustments
   - vol_multiplier_stop: 1.13 (was 1.14)
   - vol_multiplier_target: 1.47 (was 1.45)
   - min_ma_divergence: 0.00142 (was 0.00144)
   - bar_strength_threshold: 0.33 (was 0.34)
   - Removed close_position_threshold filter entirely
   - Result: +1.0357 improvement, +22 more trades

The #51 breakthrough shows that:
1. Micro-adjustments to volatility multipliers matter significantly
2. Removing overly restrictive filters lets quality trades through
3. The sweet spot is around 680+ trades with tighter risk management

For trial #52, hypothesis based on #51's success:
1. Keep the core logic that works (MA crossover + ATR volatility)
2. Fine-tune the volatility multipliers further:
   - vol_multiplier_stop: 1.12 (tighter stops for better risk/reward)
   - vol_multiplier_target: 1.48 (slightly higher targets)
3. Adjust min_ma_divergence to 0.00140 (catch a few more valid crossovers)
4. Slightly lower bar_strength_threshold to 0.32 (allow more momentum trades)
5. Add a subtle momentum confirmation: check if the bar's body is in the top/bottom
   half of its range, suggesting follow-through potential
6. Increase min_target from 0.95 to 1.00 (slightly better reward floors)
7. Adjust max_target from 2.85 to 2.95 (allow slightly higher targets on high vol)

The key insight: #51 proved that removing restrictive filters while tightening
volatility scaling works. Now we push that further with even tighter stops and
slightly higher targets, plus a very subtle momentum check (not restrictive, just
confirmatory).
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.12,
                 vol_multiplier_target: float = 1.48,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 1.00, max_target: float = 2.95,
                 min_ma_divergence: float = 0.00140,
                 bar_strength_threshold: float = 0.32,
                 price_tolerance: float = 0.0020,
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
        self.price_tolerance = price_tolerance
        self.size = size
        # Accumulate closes for MA calculations
        self.closes: list[float] = []
        # Accumulate true ranges for ATR-like volatility
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
        
        if bar_strength < self.bar_strength_threshold:
            return
        
        # Subtle momentum confirmation: check if bar body is in favorable half
        # For buys: close should be in upper half of the bar (>0.5)
        # For sells: close should be in lower half of the bar (<0.5)
        if bar_range > 0:
            close_position_in_bar = (last_bar.close - last_bar.low) / bar_range
        else:
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
        # + subtle momentum: close in upper half of bar
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and close_position_in_bar > 0.5):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
        # + subtle momentum: close in lower half of bar
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and close_position_in_bar < 0.5):
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
