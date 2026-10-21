"""
Analysis of trials #0-#91 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. All refinements to ensemble weighting, volatility regimes, and adaptive thresholds plateau at +1.54
3. The core MA crossover (6/20) with volatility-scaled brackets is proven robust
4. Trials #90-#91 explored volatility-aware thresholds but hit diminishing returns
5. What we haven't explored: ENTRY TIMING OPTIMIZATION WITHIN CONFIRMED TRENDS

Critical insight - ENTRY TIMING PRECISION:
The plateau at +1.54 suggests we're entering on confirmed signals but not at optimal prices.
Current approaches wait for ensemble confirmation THEN enter immediately. But what if we add
one more layer: once a trend is confirmed, wait for a PULLBACK within that trend before entering?

New hypothesis - TREND CONFIRMATION + PULLBACK ENTRY:
1. Keep the MA crossover as primary trend filter (fast > slow for buys, fast < slow for sells)
2. Keep the three-signal ensemble (order flow, price action, momentum) for confirmation
3. ADD: Once trend + ensemble are confirmed, wait for a pullback before entering:
   - For BUYS: Trend confirmed, then wait for price to dip closer to fast MA (within 0.15%)
   - For SELLS: Trend confirmed, then wait for price to rise closer to fast MA (within 0.15%)
4. This filters out "buy the rip" entries and "sell the dip" entries - we enter on pullbacks
5. Track "signal armed" state - when trend+ensemble align, set a flag and wait for pullback
6. Tighter parameters:
   - vol_multiplier_stop: 1.03 (tighter stops for better risk/reward on better entries)
   - vol_multiplier_target: 1.60 (wider targets to reward the better entry timing)
   - order_flow_threshold: 0.08 (slightly lower, less restrictive)
   - min_ma_divergence: 0.00120 (catch trend starts earlier)
   - pullback_threshold: 0.0015 (how close to fast MA to trigger entry)

The insight: #91's volatility-regime thresholds were good, but we're still entering on the
confirmation bar itself. What if we enter on the NEXT pullback within the confirmed trend?
This captures better prices and avoids chasing breakouts, which should improve win rate.

Expected outcome: Exceed #63's +1.5421 by entering on pullbacks within confirmed trends,
capturing better prices and improving the risk/reward of each trade.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 price_action_lookback: int = 5, momentum_lookback: int = 5,
                 vol_percentile_lookback: int = 50,
                 vol_multiplier_stop: float = 1.03,
                 vol_multiplier_target: float = 1.60,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00120,
                 order_flow_threshold: float = 0.08,
                 price_tolerance: float = 0.0021,
                 ensemble_threshold_normal: float = 1.8,
                 ensemble_threshold_high_vol: float = 1.5,
                 ensemble_threshold_low_vol: float = 2.1,
                 pullback_threshold: float = 0.0015,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
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
        self.order_flow_threshold = order_flow_threshold
        self.price_tolerance = price_tolerance
        self.ensemble_threshold_normal = ensemble_threshold_normal
        self.ensemble_threshold_high_vol = ensemble_threshold_high_vol
        self.ensemble_threshold_low_vol = ensemble_threshold_low_vol
        self.pullback_threshold = pullback_threshold
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
        self.atr_values: list[float] = []
        
        # Track armed signals waiting for pullback entry
        self.signal_armed_buy: bool = False
        self.signal_armed_sell: bool = False
        self.armed_fast_ma_buy: float | None = None
        self.armed_fast_ma_sell: float | None = None

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
        max_needed = self.slow_lookback + self.vol_lookback + self.price_action_lookback + self.momentum_lookback + self.vol_percentile_lookback + 10
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.highs) > max_needed:
            self.highs = self.highs[-max_needed:]
        if len(self.lows) > max_needed:
            self.lows = self.lows[-max_needed:]
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
            if current_price > 0:
                vol = atr / current_price
            self.atr_values.append(atr)
            if len(self.atr_values) > self.vol_percentile_lookback:
                self.atr_values = self.atr_values[-self.vol_percentile_lookback:]
        
        self.vol_history.append(vol)
        if len(self.vol_history) > self.vol_percentile_lookback:
            self.vol_history = self.vol_history[-self.vol_percentile_lookback:]
        
        # Calculate volatility regime percentile
        vol_percentile = 50  # default to normal
        if len(self.vol_history) >= self.vol_percentile_lookback:
            sorted_vols = sorted(self.vol_history)
            vol_percentile = int(100 * self.vol_history.index(sorted_vols[len(sorted_vols) // 2]) / len(self.vol_history))
        
        # Determine adaptive threshold based on volatility regime
        if vol_percentile > 75:
            ensemble_threshold = self.ensemble_threshold_high_vol
        elif vol_percentile < 25:
            ensemble_threshold = self.ensemble_threshold_low_vol
        else:
            ensemble_threshold = self.ensemble_threshold_normal
        
        # SIGNAL A: Order flow confirmation
        total_volume = last_bar.volume
        order_flow_signal = 0.0
        if total_volume > 0:
            delta = last_bar.delta
            order_flow_ratio = delta / total_volume
            if order_flow_ratio > self.order_flow_threshold:
                order_flow_signal = min(1.0, abs(order_flow_ratio) / 0.3)
            elif order_flow_ratio < -self.order_flow_threshold:
                order_flow_signal = min(1.0, abs(order_flow_ratio) / 0.3)
        
        # SIGNAL B: Price action confirmation
        price_action_signal_buy = 0.0
        price_action_signal_sell = 0.0
        
        if len(self.highs) >= self.price_action_lookback and len(self.lows) >= self.price_action_lookback:
            recent_highs = self.highs[-self.price_action_lookback:]
            recent_lows = self.lows[-self.price_action_lookback:]
            
            range_high = max(recent_highs)
            range_low = min(recent_lows)
            range_mid = (range_high + range_low) / 2.0
            
            if range_high > range_low:
                # Buy confirmation: how far above midpoint
                if current_price > range_mid:
                    price_action_signal_buy = (current_price - range_mid) / (range_high - range_mid)
                
                # Sell confirmation: how far below midpoint
                if current_price < range_mid:
                    price_action_signal_sell = (range_mid - current_price) / (range_mid - range_low)
        
        # SIGNAL C: Momentum confirmation
        momentum_signal_buy = 0.0
        momentum_signal_sell = 0.0
        
        if len(self.closes) >= self.momentum_lookback:
            recent_closes = self.closes[-self.momentum_lookback:]
            momentum_closes_above_mid = sum(1 for c in recent_closes if c > (min(recent_closes) + max(recent_closes)) / 2.0)
            momentum_closes_below_mid = self.momentum_lookback - momentum_closes_above_mid
            
            # Buy: more closes above midpoint
            momentum_signal_buy = momentum_closes_above_mid / self.momentum_lookback
            # Sell: more closes below midpoint
            momentum_signal_sell = momentum_closes_below_mid / self.momentum_lookback
        
        # Calculate MA divergence (normalized)
        ma_divergence = abs(fast_ma - slow_ma) / slow_ma
        
        # Only trade if MAs are sufficiently diverged
        if ma_divergence < self.min_ma_divergence:
            # Clear armed signals if MA divergence drops below threshold
            self.signal_armed_buy = False
            self.signal_armed_sell = False
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band
        tolerance_band = self.price_tolerance * slow_ma
        
        # BUY LOGIC: Trend confirmation + pullback entry
        if fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and last_bar.close > last_bar.open:
            buy_ensemble_score = (
                order_flow_signal +
                price_action_signal_buy +
                momentum_signal_buy
            )
            
            if buy_ensemble_score > ensemble_threshold:
                # Signal confirmed: arm the buy signal
                self.signal_armed_buy = True
                self.armed_fast_ma_buy = fast_ma
            
            # Check if pullback entry condition is met
            if self.signal_armed_buy and self.armed_fast_ma_buy is not None:
                pullback_level = self.armed_fast_ma_buy * (1.0 - self.pullback_threshold)
                if current_price <= pullback_level:
                    # Pullback occurred: enter now
                    ctx.buy(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="entry",
                    )
                    self.signal_armed_buy = False
                    self.armed_fast_ma_buy = None
        else:
            # Trend no longer confirmed: clear armed signal
            self.signal_armed_buy = False
            self.armed_fast_ma_buy = None
        
        # SELL LOGIC: Trend confirmation + pullback entry
        if fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and last_bar.close < last_bar.open:
            sell_ensemble_score = (
                order_flow_signal +
                price_action_signal_sell +
                momentum_signal_sell
            )
            
            if sell_ensemble_score > ensemble_threshold:
                # Signal confirmed: arm the sell signal
                self.signal_armed_sell = True
                self.armed_fast_ma_sell = fast_ma
            
            # Check if pullback entry condition is met
            if self.signal_armed_sell and self.armed_fast_ma_sell is not None:
                pullback_level = self.armed_fast_ma_sell * (1.0 + self.pullback_threshold)
                if current_price >= pullback_level:
                    # Pullback occurred: enter now
                    ctx.sell(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="entry",
                    )
                    self.signal_armed_sell = False
                    self.armed_fast_ma_sell = None
        else:
            # Trend no longer confirmed: clear armed signal
            self.signal_armed_sell = False
            self.armed_fast_ma_sell = None

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
