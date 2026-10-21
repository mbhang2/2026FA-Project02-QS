"""
Analysis of trials #0-#92 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. Pullback entry timing (#92) and volatility-regime thresholds (#91) both hit diminishing returns
3. The core MA crossover (6/20) with volatility-scaled brackets is proven robust
4. All ensemble refinements plateau at +1.54 - we're hitting a ceiling with current signal composition

Critical insight - SIGNAL QUALITY OVER SIGNAL COUNT:
The plateau at +1.54 suggests we're not entering on the HIGHEST CONVICTION signals. Current
ensemble weights order flow, price action, and momentum equally. But what if we RANK signals
by their individual predictive power and only enter on the STRONGEST signals?

New hypothesis - CONVICTION-BASED ENTRY FILTERING:
Instead of summing three signals and comparing to a threshold, we use a RANKING approach:

1. Keep MA crossover as primary trend filter (fast > slow for buys, fast < slow for sells)
2. Calculate THREE independent signal scores with their own thresholds:
   - Order flow conviction: delta / volume ratio, threshold 0.12 (higher bar for this signal alone)
   - Price action conviction: proximity to range extremes, threshold 0.65
   - Momentum conviction: acceleration (closes above/below midpoint), threshold 0.70
3. ONLY enter if AT LEAST TWO of the three signals exceed their INDIVIDUAL thresholds
   (This is stricter than ensemble summing: we need strong conviction in multiple dimensions)
4. Tighter volatility scaling:
   - vol_multiplier_stop: 1.02 (very tight stops, reward precision entry)
   - vol_multiplier_target: 1.65 (wider targets to compensate)
5. Remove pullback delay - go back to immediate entry on confirmed signals, but with higher bar

The insight: #92's pullback delay added complexity without improving score. The real issue is
we're entering on MARGINAL signals. What if we raise the bar for EACH signal individually and
require agreement from at least 2 of 3? This filters false breakouts more aggressively.

Expected outcome: Exceed #63's +1.5421 by entering only on the highest-conviction multi-signal
confirmations, improving win rate and reducing whipsaws.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 price_action_lookback: int = 5, momentum_lookback: int = 5,
                 vol_percentile_lookback: int = 50,
                 vol_multiplier_stop: float = 1.02,
                 vol_multiplier_target: float = 1.65,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00120,
                 order_flow_threshold: float = 0.12,
                 price_action_threshold: float = 0.65,
                 momentum_threshold: float = 0.70,
                 price_tolerance: float = 0.0021,
                 min_signals_required: int = 2,
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
        self.price_action_threshold = price_action_threshold
        self.momentum_threshold = momentum_threshold
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
            if order_flow_ratio > self.order_flow_threshold:
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
                if current_price > (range_low + range_span * self.price_action_threshold):
                    price_action_signal_buy = True
                # Sell: price in lower portion of range
                if current_price < (range_high - range_span * self.price_action_threshold):
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
            if closes_above / self.momentum_lookback > self.momentum_threshold:
                momentum_signal_buy = True
            # Sell: strong majority below midpoint
            if closes_below / self.momentum_lookback > self.momentum_threshold:
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
