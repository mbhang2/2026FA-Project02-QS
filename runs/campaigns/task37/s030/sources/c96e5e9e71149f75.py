"""
Analysis of trials #0-#90 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. All single-signal and dual-signal refinements plateau at +1.54
3. The core MA crossover (6/20) with volatility-scaled brackets is proven robust
4. Trials #87-#90 explored momentum, price action, and ensemble weighting but hit diminishing returns
5. What we haven't explored: REGIME-ADAPTIVE ENTRY FILTERING WITH VOLATILITY CLUSTERING

Critical insight - VOLATILITY CLUSTERING AND ENTRY QUALITY:
The plateau at +1.54 suggests we're entering too many marginal trades. Current approaches
use MA crossover + multiple confirmations, but don't account for VOLATILITY CLUSTERING
where high volatility periods have different win rates than low volatility periods.

New hypothesis - VOLATILITY REGIME ADAPTIVE ENTRY THRESHOLD:
Instead of a fixed ensemble score threshold, use VOLATILITY-AWARE thresholds:

1. Keep the MA crossover framework (fast > slow for buys, fast < slow for sells)
2. Keep the three-signal ensemble (order flow, price action, momentum) but ADAPT the threshold:
   - Calculate rolling volatility percentile (0-100) of recent ATR
   - In HIGH volatility (>75th percentile): require ensemble_score > 1.5 (be MORE selective)
   - In NORMAL volatility (25-75th): require ensemble_score > 1.8 (baseline)
   - In LOW volatility (<25th): require ensemble_score > 2.1 (be LESS selective, capture trends)
3. Rationale: High volatility periods have more noise and false breakouts, so be selective.
     Low volatility periods have cleaner trends, so be more aggressive.
4. Tighter parameters:
   - vol_multiplier_stop: 1.04 (aggressive stops for better risk/reward)
   - vol_multiplier_target: 1.58 (wider targets to capture more)
   - order_flow_threshold: 0.09 (slightly lower to be less restrictive)
   - min_ma_divergence: 0.00125 (catch earlier trend starts)

The insight: #90's adaptive weighting was good, but we're using fixed thresholds in
variable volatility. What if the threshold itself adapts to market volatility regime?
This filters false breakouts in choppy markets while capturing cleaner trends in quiet markets.

Expected outcome: Exceed #63's +1.5421 by using volatility-regime-aware entry thresholds
that dynamically adjust selectivity based on current market noise levels.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 price_action_lookback: int = 5, momentum_lookback: int = 5,
                 vol_percentile_lookback: int = 50,
                 vol_multiplier_stop: float = 1.04,
                 vol_multiplier_target: float = 1.58,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00125,
                 order_flow_threshold: float = 0.09,
                 price_tolerance: float = 0.0021,
                 ensemble_threshold_normal: float = 1.8,
                 ensemble_threshold_high_vol: float = 1.5,
                 ensemble_threshold_low_vol: float = 2.1,
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
            return
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend + weighted signal ensemble with adaptive threshold
        if fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and last_bar.close > last_bar.open:
            buy_ensemble_score = (
                order_flow_signal +
                price_action_signal_buy +
                momentum_signal_buy
            )
            
            if buy_ensemble_score > ensemble_threshold:
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="entry",
                )
        
        # Sell: downtrend + weighted signal ensemble with adaptive threshold
        elif fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and last_bar.close < last_bar.open:
            sell_ensemble_score = (
                order_flow_signal +
                price_action_signal_sell +
                momentum_signal_sell
            )
            
            if sell_ensemble_score > ensemble_threshold:
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
