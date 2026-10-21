"""
Analysis of trials #0-#88 and strategic refinement for breakthrough:

Best score: #63 at +1.5421 with 681 trades.

Key observations from the full progression:
1. Top performers cluster around +1.54 with 680-700 trades
2. All signal refinements (volatility scaling, order flow, momentum, trend strength) hit diminishing returns
3. The core MA crossover (6/20) with volatility-scaled brackets is proven robust
4. Trials #87-#88 added momentum acceleration and trend strength but didn't break through
5. What we haven't explored: ENTRY TIMING PRECISION WITH PRICE ACTION CONFIRMATION

Critical insight - PRICE ACTION REVERSION FILTER:
The plateau at +1.54 suggests we're entering too many false breakouts. Current approach
uses MA crossover + order flow, but doesn't filter for SUSTAINABLE momentum.

New hypothesis - HIGH/LOW REVERSAL CONFIRMATION:
Instead of just confirming on the current bar, what if we require the current bar to
show PRICE REJECTION of extremes? This filters out entries where momentum is exhausted.

1. Keep the MA crossover framework (fast > slow for buys, fast < slow for sells)
2. Keep order flow confirmation (delta/volume ratio)
3. Add PRICE ACTION REJECTION FILTER:
   - For BUY: current close must be ABOVE the midpoint of the last 3 bars' range
     (showing rejection of lows, not exhaustion at highs)
   - For SELL: current close must be BELOW the midpoint of the last 3 bars' range
     (showing rejection of highs, not exhaustion at lows)
4. This works because:
   - A breakout where price closes near the extreme of the range is likely exhausted
   - A breakout where price closes away from the extreme shows sustainable momentum
   - It filters out "wick" entries where momentum is false
5. Tighten parameters slightly:
   - vol_multiplier_stop: 1.06 (tighter stops for better risk/reward)
   - vol_multiplier_target: 1.54 (maintain target distance)
   - order_flow_threshold: 0.12 (balanced)

The insight: #63 worked well but we've been assuming all bars in MA crossover are equal.
What if we only trade when price action shows REJECTION of the opposite extreme? This
reduces whipsaws without reducing valid entries.

Expected outcome: Exceed #63's +1.5421 by filtering entries through price action rejection,
maintaining ~680 trades with better quality entries and fewer false breakouts.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 price_action_lookback: int = 3,
                 vol_multiplier_stop: float = 1.06,
                 vol_multiplier_target: float = 1.54,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00140,
                 order_flow_threshold: float = 0.12,
                 price_tolerance: float = 0.0021,
                 size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.price_action_lookback = price_action_lookback
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
        # Accumulate highs and lows for price action
        self.highs: list[float] = []
        self.lows: list[float] = []
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
        max_needed = self.slow_lookback + self.vol_lookback + self.price_action_lookback + 10
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
            # Normalize by current price to get volatility as a fraction
            if current_price > 0:
                vol = atr / current_price
        
        self.vol_history.append(vol)
        if len(self.vol_history) > self.vol_lookback * 2:
            self.vol_history = self.vol_history[-(self.vol_lookback * 2):]
        
        # Calculate price action confirmation: rejection of opposite extreme
        price_action_confirmed_buy = False
        price_action_confirmed_sell = False
        
        if len(self.highs) >= self.price_action_lookback and len(self.lows) >= self.price_action_lookback:
            recent_highs = self.highs[-self.price_action_lookback:]
            recent_lows = self.lows[-self.price_action_lookback:]
            
            range_high = max(recent_highs)
            range_low = min(recent_lows)
            range_mid = (range_high + range_low) / 2.0
            
            # Buy confirmation: close above midpoint (rejection of lows)
            if current_price > range_mid:
                price_action_confirmed_buy = True
            
            # Sell confirmation: close below midpoint (rejection of highs)
            if current_price < range_mid:
                price_action_confirmed_sell = True
        
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
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + positive order flow + price action confirmation
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and order_flow_ratio > self.order_flow_threshold and
            price_action_confirmed_buy):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="entry",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + negative order flow + price action confirmation
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and order_flow_ratio < -self.order_flow_threshold and
              price_action_confirmed_sell):
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
