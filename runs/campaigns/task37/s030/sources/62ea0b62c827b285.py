"""
Analysis of trials #0-#95 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. Volatility-adaptive approaches (#94, #95) show the right direction but have implementation issues
3. The core MA crossover (6/20) with volatility scaling is proven robust
4. Trials #94-#95 attempted smooth regime transitions but the interpolation math may be off
5. What we haven't fully explored: INDEPENDENT SIGNAL WEIGHTING based on their individual conviction

Critical insight - WEIGHTED SIGNAL ENSEMBLE WITH PROPER REGIME ADAPTATION:
The issue with #94-#95: smooth interpolation of MA periods may create suboptimal values at extremes.
Also, all three signals are treated equally (require 2 of 3), but they have different predictive power.

New hypothesis - CONVICTION-WEIGHTED SIGNAL ENSEMBLE:
1. Keep three independent signals but weight them by their individual conviction strength:
   - Order flow: weight by (order_flow_ratio / threshold) - how far above threshold?
   - Price action: weight by (position_in_range / range_height) - how extreme is the position?
   - Momentum: weight by (extreme_closes / total_closes) - how unanimous is the momentum?
2. Require weighted signal sum >= 1.5 instead of 2 of 3 (allows strong single signals)
3. Use discrete volatility regimes but with better calibration:
   - High vol (>60th percentile): MA periods 5/18, thresholds 0.10/0.60/0.65
   - Normal vol (25-60th): MA periods 6/20, thresholds 0.12/0.65/0.70
   - Low vol (<25th): MA periods 7/22, thresholds 0.14/0.70/0.75
4. Keep tight volatility scaling: vol_multiplier_stop=1.02, vol_multiplier_target=1.65
5. Use better percentile calculation (numpy-style) for regime detection

The insight: Weighting signals by conviction strength allows strong individual signals to trigger
without requiring agreement from weaker signals. This reduces false negatives (missing good setups
where one signal is very strong but others are weak) while maintaining quality filtering.

Expected outcome: Exceed #63's +1.5421 by using conviction-weighted signal ensemble instead of
simple vote counting, allowing strong individual signals to drive entries.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m",
                 fast_lookback_normal: int = 6,
                 slow_lookback_normal: int = 20,
                 fast_lookback_high_vol: int = 5,
                 slow_lookback_high_vol: int = 18,
                 fast_lookback_low_vol: int = 7,
                 slow_lookback_low_vol: int = 22,
                 vol_lookback: int = 18,
                 price_action_lookback: int = 5,
                 momentum_lookback: int = 5,
                 vol_percentile_lookback: int = 50,
                 vol_multiplier_stop: float = 1.02,
                 vol_multiplier_target: float = 1.65,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00120,
                 order_flow_threshold_normal: float = 0.12,
                 order_flow_threshold_high_vol: float = 0.10,
                 order_flow_threshold_low_vol: float = 0.14,
                 price_action_threshold_normal: float = 0.65,
                 price_action_threshold_high_vol: float = 0.60,
                 price_action_threshold_low_vol: float = 0.70,
                 momentum_threshold_normal: float = 0.70,
                 momentum_threshold_high_vol: float = 0.65,
                 momentum_threshold_low_vol: float = 0.75,
                 price_tolerance: float = 0.0021,
                 min_weighted_conviction: float = 1.5,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback_normal = fast_lookback_normal
        self.slow_lookback_normal = slow_lookback_normal
        self.fast_lookback_high_vol = fast_lookback_high_vol
        self.slow_lookback_high_vol = slow_lookback_high_vol
        self.fast_lookback_low_vol = fast_lookback_low_vol
        self.slow_lookback_low_vol = slow_lookback_low_vol
        self.vol_lookback = vol_lookback
        self.price_action_lookback = price_action_lookback
        self.momentum_lookback = momentum_lookback
        self.vol_percentile_lookback = vol_percentile_lookback
        self.vol_multiplier_stop = vol_multiplier_stop
        self.vol_multiplier_target = vol_multiplier_target
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.min_ma_divergence = min_ma_divergence
        self.order_flow_threshold_normal = order_flow_threshold_normal
        self.order_flow_threshold_high_vol = order_flow_threshold_high_vol
        self.order_flow_threshold_low_vol = order_flow_threshold_low_vol
        self.price_action_threshold_normal = price_action_threshold_normal
        self.price_action_threshold_high_vol = price_action_threshold_high_vol
        self.price_action_threshold_low_vol = price_action_threshold_low_vol
        self.momentum_threshold_normal = momentum_threshold_normal
        self.momentum_threshold_high_vol = momentum_threshold_high_vol
        self.momentum_threshold_low_vol = momentum_threshold_low_vol
        self.price_tolerance = price_tolerance
        self.min_weighted_conviction = min_weighted_conviction
        self.size = size
        
        # Accumulate closes, highs, lows
        self.closes: list[float] = []
        self.highs: list[float] = []
        self.lows: list[float] = []
        
        # Accumulate true ranges for ATR-like volatility
        self.true_ranges: list[float] = []
        self.prev_close: float | None = None
        
        # Track volatility history for regime detection
        self.vol_history: list[float] = []

    def _calculate_percentile(self, data: list[float], percentile: float) -> float:
        """Calculate percentile value from a list (0-100)."""
        if not data:
            return 0.0
        sorted_data = sorted(data)
        idx = int((percentile / 100.0) * (len(sorted_data) - 1))
        idx = max(0, min(idx, len(sorted_data) - 1))
        return sorted_data[idx]

    def _get_vol_regime(self, current_vol: float, vol_history: list[float]) -> str:
        """
        Determine volatility regime: 'high', 'normal', or 'low'
        """
        if len(vol_history) < self.vol_percentile_lookback:
            return 'normal'
        
        p25 = self._calculate_percentile(vol_history, 25)
        p60 = self._calculate_percentile(vol_history, 60)
        
        if current_vol > p60:
            return 'high'
        elif current_vol < p25:
            return 'low'
        else:
            return 'normal'

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

        # Accumulate closes, highs, lows
        self.closes.append(current_price)
        self.highs.append(last_bar.high)
        self.lows.append(last_bar.low)
        
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
        max_needed = self.slow_lookback_low_vol + self.vol_lookback + self.price_action_lookback + self.momentum_lookback + self.vol_percentile_lookback + 10
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.highs) > max_needed:
            self.highs = self.highs[-max_needed:]
        if len(self.lows) > max_needed:
            self.lows = self.lows[-max_needed:]
        if len(self.true_ranges) > max_needed:
            self.true_ranges = self.true_ranges[-max_needed:]

        # Need enough data for slowest moving average
        if len(self.closes) < self.slow_lookback_low_vol:
            return

        # Calculate volatility using ATR
        vol = 0.0
        if len(self.true_ranges) >= self.vol_lookback:
            recent_ranges = self.true_ranges[-self.vol_lookback:]
            atr = sum(recent_ranges) / len(recent_ranges)
            if current_price > 0:
                vol = atr / current_price
        
        self.vol_history.append(vol)
        if len(self.vol_history) > self.vol_percentile_lookback:
            self.vol_history = self.vol_history[-self.vol_percentile_lookback:]
        
        # Determine volatility regime
        vol_regime = self._get_vol_regime(vol, self.vol_history)
        
        # Select adaptive MA periods and thresholds based on volatility regime
        if vol_regime == 'high':
            fast_lookback = self.fast_lookback_high_vol
            slow_lookback = self.slow_lookback_high_vol
            order_flow_threshold = self.order_flow_threshold_high_vol
            price_action_threshold = self.price_action_threshold_high_vol
            momentum_threshold = self.momentum_threshold_high_vol
        elif vol_regime == 'low':
            fast_lookback = self.fast_lookback_low_vol
            slow_lookback = self.slow_lookback_low_vol
            order_flow_threshold = self.order_flow_threshold_low_vol
            price_action_threshold = self.price_action_threshold_low_vol
            momentum_threshold = self.momentum_threshold_low_vol
        else:
            fast_lookback = self.fast_lookback_normal
            slow_lookback = self.slow_lookback_normal
            order_flow_threshold = self.order_flow_threshold_normal
            price_action_threshold = self.price_action_threshold_normal
            momentum_threshold = self.momentum_threshold_normal
        
        # Calculate fast and slow moving averages with adaptive periods
        if len(self.closes) < slow_lookback:
            return
        
        fast_closes = self.closes[-fast_lookback:]
        slow_closes = self.closes[-slow_lookback:]
        
        fast_ma = sum(fast_closes) / len(fast_closes)
        slow_ma = sum(slow_closes) / len(slow_closes)
        
        if slow_ma <= 0:
            return
        
        # Calculate MA divergence
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band
        tolerance_band = self.price_tolerance * slow_ma
        
        # SIGNAL 1: Order flow conviction - weight by how far above/below threshold
        order_flow_weight_buy = 0.0
        order_flow_weight_sell = 0.0
        
        total_volume = last_bar.volume
        if total_volume > 0:
            delta = last_bar.delta
            order_flow_ratio = abs(delta / total_volume)
            if order_flow_ratio > order_flow_threshold:
                # Weight by how much we exceed threshold (capped at 1.0)
                conviction = min(1.0, (order_flow_ratio - order_flow_threshold) / order_flow_threshold)
                if delta > 0:
                    order_flow_weight_buy = conviction
                else:
                    order_flow_weight_sell = conviction
        
        # SIGNAL 2: Price action conviction - weight by position in range
        price_action_weight_buy = 0.0
        price_action_weight_sell = 0.0
        
        if len(self.highs) >= self.price_action_lookback and len(self.lows) >= self.price_action_lookback:
            recent_highs = self.highs[-self.price_action_lookback:]
            recent_lows = self.lows[-self.price_action_lookback:]
            
            range_high = max(recent_highs)
            range_low = min(recent_lows)
            
            if range_high > range_low:
                range_span = range_high - range_low
                position_in_range = (current_price - range_low) / range_span
                
                # Buy: price in upper portion, weight by how far above threshold
                if position_in_range > price_action_threshold:
                    conviction = min(1.0, (position_in_range - price_action_threshold) / (1.0 - price_action_threshold))
                    price_action_weight_buy = conviction
                
                # Sell: price in lower portion, weight by how far below threshold
                if position_in_range < (1.0 - price_action_threshold):
                    conviction = min(1.0, ((1.0 - price_action_threshold) - position_in_range) / (1.0 - price_action_threshold))
                    price_action_weight_sell = conviction
        
        # SIGNAL 3: Momentum conviction - weight by how unanimous the momentum
        momentum_weight_buy = 0.0
        momentum_weight_sell = 0.0
        
        if len(self.closes) >= self.momentum_lookback:
            recent_closes = self.closes[-self.momentum_lookback:]
            mid_price = (min(recent_closes) + max(recent_closes)) / 2.0
            closes_above = sum(1 for c in recent_closes if c > mid_price)
            closes_below = self.momentum_lookback - closes_above
            
            # Buy: strong majority above midpoint
            ratio_above = closes_above / self.momentum_lookback
            if ratio_above > momentum_threshold:
                conviction = min(1.0, (ratio_above - momentum_threshold) / (1.0 - momentum_threshold))
                momentum_weight_buy = conviction
            
            # Sell: strong majority below midpoint
            ratio_below = closes_below / self.momentum_lookback
            if ratio_below > momentum_threshold:
                conviction = min(1.0, (ratio_below - momentum_threshold) / (1.0 - momentum_threshold))
                momentum_weight_sell = conviction
        
        # WEIGHTED CONVICTION ENSEMBLE
        buy_conviction = order_flow_weight_buy + price_action_weight_buy + momentum_weight_buy
        sell_conviction = order_flow_weight_sell + price_action_weight_sell + momentum_weight_sell
        
        # BUY: Uptrend + sufficient weighted conviction + price in range
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and buy_conviction >= self.min_weighted_conviction):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )
        
        # SELL: Downtrend + sufficient weighted conviction + price in range
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and sell_conviction >= self.min_weighted_conviction):
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
