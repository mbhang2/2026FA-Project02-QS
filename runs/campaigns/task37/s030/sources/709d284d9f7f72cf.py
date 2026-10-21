"""
Analysis of trials #0-#98 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. Trial #98 uses regime-aware conviction thresholds (high_vol=1.2, normal=1.0, low_vol=0.8)
3. Conviction signals can express up to 1.5 (order flow, price action, momentum)
4. Volatility regime detection uses p20/p65 percentiles for more extreme regime detection
5. The current approach with 0.9 power scaling on price action and momentum is working well

Critical insight - SIGNAL CALIBRATION REFINEMENT WITH BETTER THRESHOLD SCALING:
Trial #98 allows signals to express conviction up to 1.5, but the thresholds for each signal
may not be optimally calibrated. The order flow threshold (0.12 normal) might be too high,
filtering out valid signals. Price action threshold (0.65 normal) might be slightly too high.

New hypothesis - REFINED THRESHOLD CALIBRATION WITH ENHANCED SIGNAL EXPRESSION:
1. Lower order flow thresholds across all regimes:
   - High vol: 0.09 (was 0.10) - catch stronger order flow signals in noise
   - Normal vol: 0.10 (was 0.12) - lower the bar slightly
   - Low vol: 0.12 (was 0.14) - still selective in low vol
2. Slightly adjust price action thresholds:
   - High vol: 0.62 (was 0.60) - slightly more selective in noise
   - Normal vol: 0.63 (was 0.65) - lower the bar slightly
   - Low vol: 0.68 (was 0.70) - slightly less selective in low vol
3. Adjust momentum thresholds:
   - High vol: 0.67 (was 0.65) - more selective in noisy conditions
   - Normal vol: 0.68 (was 0.70) - lower the bar slightly
   - Low vol: 0.73 (was 0.75) - slightly less selective in low vol
4. Increase conviction cap from 1.5 to 1.8 to allow stronger signals more expression
5. Refine min_ma_divergence from 0.00120 to 0.00110 for slightly more entries
6. Adjust vol_multiplier_stop to 0.95 (was 0.98) for tighter risk management
7. Keep vol_multiplier_target at 1.65 for asymmetric reward
8. Adjust regime percentile thresholds: p22/p62 instead of p20/p65 for better calibration

The insight: The thresholds in #98 are slightly too high, filtering out valid signals.
By lowering order flow and price action thresholds while increasing conviction cap to 1.8,
we allow more high-quality entries while maintaining selectivity through conviction weighting.
The regime detection adjustment (p22/p62) provides better middle-ground regime classification.

Expected outcome: Exceed #63's +1.5421 by allowing more valid entries through refined
threshold calibration while maintaining signal quality.
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
                 vol_multiplier_stop: float = 0.95,
                 vol_multiplier_target: float = 1.65,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00110,
                 order_flow_threshold_normal: float = 0.10,
                 order_flow_threshold_high_vol: float = 0.09,
                 order_flow_threshold_low_vol: float = 0.12,
                 price_action_threshold_normal: float = 0.63,
                 price_action_threshold_high_vol: float = 0.62,
                 price_action_threshold_low_vol: float = 0.68,
                 momentum_threshold_normal: float = 0.68,
                 momentum_threshold_high_vol: float = 0.67,
                 momentum_threshold_low_vol: float = 0.73,
                 price_tolerance: float = 0.0021,
                 min_weighted_conviction_high_vol: float = 1.2,
                 min_weighted_conviction_normal: float = 1.0,
                 min_weighted_conviction_low_vol: float = 0.8,
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
        self.min_weighted_conviction_high_vol = min_weighted_conviction_high_vol
        self.min_weighted_conviction_normal = min_weighted_conviction_normal
        self.min_weighted_conviction_low_vol = min_weighted_conviction_low_vol
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
        Using p22/p62 thresholds for better middle-ground regime detection.
        """
        if len(vol_history) < self.vol_percentile_lookback:
            return 'normal'
        
        p22 = self._calculate_percentile(vol_history, 22)
        p62 = self._calculate_percentile(vol_history, 62)
        
        if current_vol > p62:
            return 'high'
        elif current_vol < p22:
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
            min_weighted_conviction = self.min_weighted_conviction_high_vol
        elif vol_regime == 'low':
            fast_lookback = self.fast_lookback_low_vol
            slow_lookback = self.slow_lookback_low_vol
            order_flow_threshold = self.order_flow_threshold_low_vol
            price_action_threshold = self.price_action_threshold_low_vol
            momentum_threshold = self.momentum_threshold_low_vol
            min_weighted_conviction = self.min_weighted_conviction_low_vol
        else:
            fast_lookback = self.fast_lookback_normal
            slow_lookback = self.slow_lookback_normal
            order_flow_threshold = self.order_flow_threshold_normal
            price_action_threshold = self.price_action_threshold_normal
            momentum_threshold = self.momentum_threshold_normal
            min_weighted_conviction = self.min_weighted_conviction_normal
        
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
        
        # SIGNAL 1: Order flow conviction - allow conviction up to 1.8
        order_flow_weight_buy = 0.0
        order_flow_weight_sell = 0.0
        
        total_volume = last_bar.volume
        if total_volume > 0:
            delta = last_bar.delta
            order_flow_ratio = abs(delta / total_volume)
            if order_flow_ratio > order_flow_threshold:
                # Refined scaling: allow conviction to reach 1.8 for very strong signals
                conviction = min(1.8, (order_flow_ratio - order_flow_threshold) / order_flow_threshold)
                if delta > 0:
                    order_flow_weight_buy = conviction
                else:
                    order_flow_weight_sell = conviction
        
        # SIGNAL 2: Price action conviction - refined with 0.9 power scaling
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
                
                # Buy: price in upper portion, 0.9 power for slightly smoother scaling
                if position_in_range > price_action_threshold:
                    excess = position_in_range - price_action_threshold
                    max_excess = 1.0 - price_action_threshold
                    conviction = min(1.8, (excess / max_excess) ** 0.9)
                    price_action_weight_buy = conviction
                
                # Sell: price in lower portion, 0.9 power scaling
                if position_in_range < (1.0 - price_action_threshold):
                    excess = (1.0 - price_action_threshold) - position_in_range
                    max_excess = 1.0 - price_action_threshold
                    conviction = min(1.8, (excess / max_excess) ** 0.9)
                    price_action_weight_sell = conviction
        
        # SIGNAL 3: Momentum conviction - refined scaling with 0.9 power
        momentum_weight_buy = 0.0
        momentum_weight_sell = 0.0
        
        if len(self.closes) >= self.momentum_lookback:
            recent_closes = self.closes[-self.momentum_lookback:]
            mid_price = (min(recent_closes) + max(recent_closes)) / 2.0
            closes_above = sum(1 for c in recent_closes if c > mid_price)
            closes_below = self.momentum_lookback - closes_above
            
            # Buy: strong majority above midpoint, 0.9 power scaling
            ratio_above = closes_above / self.momentum_lookback
            if ratio_above > momentum_threshold:
                excess = ratio_above - momentum_threshold
                max_excess = 1.0 - momentum_threshold
                conviction = min(1.8, (excess / max_excess) ** 0.9)
                momentum_weight_buy = conviction
            
            # Sell: strong majority below midpoint, 0.9 power scaling
            ratio_below = closes_below / self.momentum_lookback
            if ratio_below > momentum_threshold:
                excess = ratio_below - momentum_threshold
                max_excess = 1.0 - momentum_threshold
                conviction = min(1.8, (excess / max_excess) ** 0.9)
                momentum_weight_sell = conviction
        
        # WEIGHTED CONVICTION ENSEMBLE with regime-aware thresholds
        buy_conviction = order_flow_weight_buy + price_action_weight_buy + momentum_weight_buy
        sell_conviction = order_flow_weight_sell + price_action_weight_sell + momentum_weight_sell
        
        # BUY: Uptrend + sufficient weighted conviction + price in range
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and buy_conviction >= min_weighted_conviction):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )
        
        # SELL: Downtrend + sufficient weighted conviction + price in range
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and sell_conviction >= min_weighted_conviction):
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
