"""
Analysis of trials #0-#94 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. Volatility-adaptive MA periods (#94) showed promise but needs refinement
3. The core insight is correct: MA periods should adapt to market regime
4. However, the volatility percentile calculation in #94 was flawed (using index instead of actual percentile)
5. Current thresholds may be too aggressive - we're filtering out valid entries

Critical insight - VOLATILITY REGIME WITH BETTER PERCENTILE CALCULATION:
The bug in #94: percentile calculation used list.index() which doesn't give true percentile.
Also, the regime boundaries (25/75) may be too strict. What if we use smoother transitions?

New hypothesis - CORRECTED VOLATILITY-ADAPTIVE MA WITH SMOOTH REGIME TRANSITIONS:
1. Fix the volatility percentile calculation to use actual quantiles
2. Use smoother regime transitions instead of hard boundaries:
   - Instead of discrete high/normal/low at 75/25, use continuous interpolation
   - This creates smoother MA period and threshold adjustments across the vol spectrum
3. Keep the three-signal conviction model but with better regime-aware thresholds
4. Slightly relax the order flow threshold (0.11 instead of 0.12 baseline) - we may be too selective
5. Improve price action calculation: use recent range instead of just lookback window
6. Keep tight volatility scaling: vol_multiplier_stop=1.02, vol_multiplier_target=1.65

The insight: #94's approach was right but the implementation had bugs. With correct percentile
calculation and smoother transitions, we should see better signal generation without being
overly restrictive. The baseline thresholds were slightly too high.

Expected outcome: Fix #94's bugs and exceed +1.5421 by properly adapting to volatility regime
with smooth transitions and slightly relaxed entry thresholds.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m",
                 fast_lookback_base: int = 6,
                 slow_lookback_base: int = 20,
                 vol_lookback: int = 18,
                 price_action_lookback: int = 5,
                 momentum_lookback: int = 5,
                 vol_percentile_lookback: int = 50,
                 vol_multiplier_stop: float = 1.02,
                 vol_multiplier_target: float = 1.65,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00120,
                 order_flow_threshold_base: float = 0.11,
                 price_action_threshold_base: float = 0.65,
                 momentum_threshold_base: float = 0.70,
                 price_tolerance: float = 0.0021,
                 min_signals_required: int = 2,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback_base = fast_lookback_base
        self.slow_lookback_base = slow_lookback_base
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
        self.order_flow_threshold_base = order_flow_threshold_base
        self.price_action_threshold_base = price_action_threshold_base
        self.momentum_threshold_base = momentum_threshold_base
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

    def _calculate_percentile(self, data: list[float], percentile: float) -> float:
        """Calculate percentile value from a list (0-100)."""
        if not data:
            return 0.0
        sorted_data = sorted(data)
        idx = int((percentile / 100.0) * (len(sorted_data) - 1))
        idx = max(0, min(idx, len(sorted_data) - 1))
        return sorted_data[idx]

    def _get_vol_regime_factor(self, current_vol: float, vol_history: list[float]) -> float:
        """
        Get a continuous regime factor from 0 to 1 based on volatility percentile.
        0 = low vol regime, 0.5 = normal vol regime, 1 = high vol regime
        """
        if len(vol_history) < self.vol_percentile_lookback:
            return 0.5  # Default to normal regime
        
        p25 = self._calculate_percentile(vol_history, 25)
        p75 = self._calculate_percentile(vol_history, 75)
        
        if p75 <= p25:
            return 0.5
        
        # Map current vol to 0-1 range based on percentile positions
        if current_vol <= p25:
            return 0.0  # Low vol
        elif current_vol >= p75:
            return 1.0  # High vol
        else:
            # Smooth interpolation between low and high
            return (current_vol - p25) / (p75 - p25)

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
        max_needed = self.slow_lookback_base + self.vol_lookback + self.price_action_lookback + self.momentum_lookback + self.vol_percentile_lookback + 10
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
        if len(self.highs) > max_needed:
            self.highs = self.highs[-max_needed:]
        if len(self.lows) > max_needed:
            self.lows = self.lows[-max_needed:]
        if len(self.true_ranges) > max_needed:
            self.true_ranges = self.true_ranges[-max_needed:]

        # Need enough data for slowest moving average
        if len(self.closes) < self.slow_lookback_base:
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
        
        # Get volatility regime factor (0=low, 0.5=normal, 1=high)
        vol_regime = self._get_vol_regime_factor(vol, self.vol_history)
        
        # Adapt MA periods based on vol regime (smooth interpolation)
        # High vol: faster MAs (5/18), Normal: base (6/20), Low vol: slower MAs (7/22)
        fast_lookback = int(self.fast_lookback_base - 1 * vol_regime + 1 * (1 - vol_regime))
        slow_lookback = int(self.slow_lookback_base - 2 * vol_regime + 2 * (1 - vol_regime))
        
        # Clamp to reasonable ranges
        fast_lookback = max(4, min(8, fast_lookback))
        slow_lookback = max(16, min(24, slow_lookback))
        
        # Adapt thresholds based on vol regime
        # High vol: lower thresholds (be more eager), Low vol: higher thresholds (be more selective)
        order_flow_threshold = self.order_flow_threshold_base - 0.02 * vol_regime + 0.03 * (1 - vol_regime)
        price_action_threshold = self.price_action_threshold_base - 0.05 * vol_regime + 0.05 * (1 - vol_regime)
        momentum_threshold = self.momentum_threshold_base - 0.05 * vol_regime + 0.05 * (1 - vol_regime)
        
        # Clamp thresholds to valid ranges
        order_flow_threshold = max(0.08, min(0.16, order_flow_threshold))
        price_action_threshold = max(0.55, min(0.75, price_action_threshold))
        momentum_threshold = max(0.60, min(0.80, momentum_threshold))
        
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
