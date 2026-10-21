"""
Analysis of trials #0-#93 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. All signal ensemble refinements plateau at +1.54 - we're hitting a ceiling
3. The core MA crossover (6/20) with volatility-scaled brackets is proven robust
4. Trials #92-#93 explored pullback delays and conviction-based filtering but hit diminishing returns
5. What we haven't explored: ADAPTIVE MA PERIODS based on volatility regime

Critical insight - MARKET REGIME ADAPTATION:
The plateau at +1.54 suggests the fixed MA periods (6/20) are optimal for AVERAGE market conditions,
but suboptimal for EXTREME conditions. In high volatility, shorter MAs might catch trends faster.
In low volatility, longer MAs might filter more noise. Current approach uses fixed lookbacks.

New hypothesis - VOLATILITY-ADAPTIVE MA PERIODS:
1. Keep the core MA crossover logic but make the periods DYNAMIC:
   - High volatility (>75th percentile): Use shorter fast (5) and slower slow (18) for faster response
   - Normal volatility (25-75th percentile): Use baseline fast (6) and slow (20)
   - Low volatility (<25th percentile): Use longer fast (7) and slow (22) for noise filtering
2. Keep the three-signal conviction model from #93 but adapt thresholds too:
   - High vol: Lower thresholds (0.10 order flow, 0.60 price action, 0.65 momentum) - be more eager
   - Normal vol: Baseline thresholds (0.12, 0.65, 0.70)
   - Low vol: Higher thresholds (0.14, 0.70, 0.75) - be more selective
3. Keep tight volatility scaling: vol_multiplier_stop=1.02, vol_multiplier_target=1.65
4. Require 2 of 3 signals as in #93

The insight: #63 succeeded with fixed 6/20, but that's one point in the vol spectrum. What if we
adapt the MA periods to the market's actual volatility regime? This could catch trend starts faster
in high vol and filter noise better in low vol, pushing past +1.54.

Expected outcome: Exceed #63's +1.5421 by adapting MA periods to volatility regime, catching
trends more efficiently in all market conditions.
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
                 min_signals_required: int = 2,
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
        self.min_signals_required = min_signals_required
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
        
        # Determine volatility regime percentile
        vol_percentile = 50
        if len(self.vol_history) >= self.vol_percentile_lookback:
            sorted_vols = sorted(self.vol_history)
            vol_percentile = int(100 * sorted_vols.index(sorted_vols[len(sorted_vols) // 2]) / len(sorted_vols))
        
        # Select adaptive MA periods and thresholds based on volatility regime
        if vol_percentile > 75:
            # High volatility regime
            fast_lookback = self.fast_lookback_high_vol
            slow_lookback = self.slow_lookback_high_vol
            order_flow_threshold = self.order_flow_threshold_high_vol
            price_action_threshold = self.price_action_threshold_high_vol
            momentum_threshold = self.momentum_threshold_high_vol
        elif vol_percentile < 25:
            # Low volatility regime
            fast_lookback = self.fast_lookback_low_vol
            slow_lookback = self.slow_lookback_low_vol
            order_flow_threshold = self.order_flow_threshold_low_vol
            price_action_threshold = self.price_action_threshold_low_vol
            momentum_threshold = self.momentum_threshold_low_vol
        else:
            # Normal volatility regime
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
        
        # SIGNAL 1: Order flow conviction (individual threshold)
        order_flow_signal_buy = False
        order_flow_signal_sell = False
        
        total_volume = last_bar.volume
        if total_volume > 0:
            delta = last_bar.delta
            order_flow_ratio = abs(delta / total_volume)
            if order_flow_ratio > order_flow_threshold:
                if delta > 0:
                    order_flow_signal_buy = True
                else:
                    order_flow_signal_sell = True
        
        # SIGNAL 2: Price action conviction (individual threshold)
        price_action_signal_buy = False
        price_action_signal_sell = False
        
        if len(self.highs) >= self.price_action_lookback and len(self.lows) >= self.price_action_lookback:
            recent_highs = self.highs[-self.price_action_lookback:]
            recent_lows = self.lows[-self.price_action_lookback:]
            
            range_high = max(recent_highs)
            range_low = min(recent_lows)
            
            if range_high > range_low:
                range_span = range_high - range_low
                # Buy: price in upper portion of range
                if current_price > (range_low + range_span * price_action_threshold):
                    price_action_signal_buy = True
                # Sell: price in lower portion of range
                if current_price < (range_high - range_span * price_action_threshold):
                    price_action_signal_sell = True
        
        # SIGNAL 3: Momentum conviction (individual threshold)
        momentum_signal_buy = False
        momentum_signal_sell = False
        
        if len(self.closes) >= self.momentum_lookback:
            recent_closes = self.closes[-self.momentum_lookback:]
            mid_price = (min(recent_closes) + max(recent_closes)) / 2.0
            closes_above = sum(1 for c in recent_closes if c > mid_price)
            closes_below = self.momentum_lookback - closes_above
            
            # Buy: strong majority above midpoint
            if closes_above / self.momentum_lookback > momentum_threshold:
                momentum_signal_buy = True
            # Sell: strong majority below midpoint
            if closes_below / self.momentum_lookback > momentum_threshold:
                momentum_signal_sell = True
        
        # COUNT SIGNALS FOR BUY
        buy_signals = sum([order_flow_signal_buy, price_action_signal_buy, momentum_signal_buy])
        
        # COUNT SIGNALS FOR SELL
        sell_signals = sum([order_flow_signal_sell, price_action_signal_sell, momentum_signal_sell])
        
        # BUY: Uptrend + at least min_signals_required signals + price in range
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and buy_signals >= self.min_signals_required):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )
        
        # SELL: Downtrend + at least min_signals_required signals + price in range
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and sell_signals >= self.min_signals_required):
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
