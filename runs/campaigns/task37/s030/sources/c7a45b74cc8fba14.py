"""
Analysis of trials #51-#56 and deeper optimization:

Best score: #56 at +1.5407 with 683 trades.
Recent pattern: Small parameter tweaks around the core MA+ATR+bar_strength strategy
yield marginal improvements (+0.02 to +0.04 per trial).

Key insight from the data:
- #51: +1.5208 (682 trades) - baseline proven strategy
- #56: +1.5407 (683 trades) - +0.0199 improvement with vol_mult tuning
- The strategy is stable and near-optimal at this parameter set

Hypothesis for breakthrough:
The current strategy is hitting diminishing returns on parameter tuning because
the core logic is already sound. To break through +1.54, we need to consider:

1. MARKET REGIME DETECTION: The volatility multipliers are fixed, but markets
   have different regimes. When volatility is very low, our stops/targets might
   be too wide. When volatility spikes, they might be too tight.
   
2. ADAPTIVE THRESHOLDS: Instead of fixed bar_strength_threshold (0.32), use
   a dynamic threshold based on recent volatility. High vol = require stronger bars.
   
3. VOLUME/MOMENTUM CONFIRMATION: The delta field on bars tells us order flow.
   A bar that closed up with positive delta is stronger than one with negative delta.
   
4. MULTI-TIMEFRAME CONFIRMATION: Add a secondary confirmation from a longer timeframe
   (like 15m) to filter out false crossovers in the 5m.

For trial #57, implement:
- Keep the proven MA crossover + ATR volatility scaling
- Add bar delta confirmation (positive delta for long, negative for short)
- Slightly tighten the vol_multiplier_stop (1.11) for better risk/reward
- Slightly increase vol_multiplier_target (1.49) to catch more upside
- Lower bar_strength_threshold to 0.30 (less restrictive, more trades)
- Add a simple momentum check: require the bar to be above/below its 6-bar average
  to confirm we're in a trending move, not just a spike

The key: We're not adding complexity, just using the data we already have (delta)
to confirm the signal quality.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.11,
                 vol_multiplier_target: float = 1.49,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00140,
                 bar_strength_threshold: float = 0.30,
                 price_tolerance: float = 0.0021,
                 delta_threshold: float = 0.15,
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
        self.delta_threshold = delta_threshold
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
        
        # Calculate delta confirmation (order flow)
        # delta = buy_volume - sell_volume
        total_volume = last_bar.buy_volume + last_bar.sell_volume
        if total_volume <= 0:
            return
        
        delta_ratio = last_bar.delta / total_volume
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
        # + positive delta confirmation (more buying pressure)
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and delta_ratio > self.delta_threshold):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
        # + negative delta confirmation (more selling pressure)
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and delta_ratio < -self.delta_threshold):
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
