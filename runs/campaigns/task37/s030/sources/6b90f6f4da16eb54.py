"""
Analysis of trials #30-#34 and optimization strategy:

The best performer (#30) scored +1.3269 with 653 trades. All recent trials
(#30-#34) cluster around +1.32-1.33, suggesting we've hit a local maximum in
the current parameter space.

Key observations from the trial sequence:
1. Trade frequency of 645-653 is optimal (lower/higher both hurt)
2. Fast MA (6) + Slow MA (20) is the proven core
3. Asymmetric vol scaling (1.14/1.45) is robust
4. Bar strength (0.38-0.39) and price tolerance (0.0015) are well-tuned
5. Adding complexity (regime filtering, momentum) hasn't improved scores

The pattern suggests diminishing returns from parameter tuning. For trial #35,
I'll test a different hypothesis: what if the issue is *position management*
rather than entry quality?

Current approach: One position at a time, held until bracket fills.
New hypothesis: Allow multiple positions with tighter, more aggressive brackets,
and use a scale-in strategy on strong momentum.

Additionally:
1. Keep the proven entry core (fast=6, slow=20, bar_strength=0.38)
2. Reduce bracket sizes slightly (stop=0.65, target=0.90) for faster exits
3. Allow up to 2 positions (scale-in on momentum confirmation)
4. Add a scale-in filter: only scale if price moved favorably after first entry
5. Use smaller size for scale-in (0.5 vs 1.0 for base)

This targets a different dimension: position structure and exit velocity.
Tighter brackets should increase trade frequency and win rate, while scale-in
captures strong momentum moves without adding whipsaw risk.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.14,
                 vol_multiplier_target: float = 1.45,
                 min_stop: float = 0.65, max_stop: float = 1.3,
                 min_target: float = 0.90, max_target: float = 2.2,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.38,
                 price_tolerance: float = 0.0015,
                 max_positions: int = 2,
                 scale_in_threshold: float = 0.0008,
                 base_size: int = 1,
                 scale_size: int = 1):
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
        self.max_positions = max_positions
        self.scale_in_threshold = scale_in_threshold
        self.base_size = base_size
        self.scale_size = scale_size
        
        # Accumulate closes for MA and volatility calculations
        self.closes: list[float] = []
        # Track returns for volatility
        self.returns: list[float] = []
        # Track entry prices for scale-in logic
        self.entry_prices: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last_bar = history[-1]
        current_price = last_bar.close

        # Accumulate closes
        self.closes.append(current_price)
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + 5
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]

        # Calculate returns for volatility
        if len(self.closes) >= 2:
            prev_price = self.closes[-2]
            if prev_price > 0:
                ret = (current_price - prev_price) / prev_price
                self.returns.append(ret)
                if len(self.returns) > self.vol_lookback + 5:
                    self.returns = self.returns[-(self.vol_lookback + 5):]

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
        
        # Calculate volatility (standard deviation of recent returns)
        vol = 0.0
        if len(self.returns) >= self.vol_lookback:
            recent_returns = self.returns[-self.vol_lookback:]
            mean_ret = sum(recent_returns) / len(recent_returns)
            variance = sum((r - mean_ret) ** 2 for r in recent_returns) / len(recent_returns)
            vol = math.sqrt(max(variance, 0.0))
        
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
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Check if we can enter new positions
        current_position = ctx.position
        num_working_orders = len(ctx.working_order_ids)
        
        # Allow scale-in if we have room and price moved favorably
        can_scale_in = False
        if current_position > 0 and len(self.entry_prices) > 0:
            # Check if price moved up favorably for long position
            avg_entry = sum(self.entry_prices) / len(self.entry_prices)
            if current_price > avg_entry * (1.0 + self.scale_in_threshold):
                can_scale_in = True
        elif current_position < 0 and len(self.entry_prices) > 0:
            # Check if price moved down favorably for short position
            avg_entry = sum(self.entry_prices) / len(self.entry_prices)
            if current_price < avg_entry * (1.0 - self.scale_in_threshold):
                can_scale_in = True
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            if current_position == 0 and num_working_orders == 0:
                # Base entry
                oid = ctx.buy(
                    size=self.base_size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-long-base",
                )
                if oid is not None:
                    self.entry_prices = [current_price]
            elif can_scale_in and current_position < self.max_positions:
                # Scale-in entry
                ctx.buy(
                    size=self.scale_size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-long-scale",
                )
                self.entry_prices.append(current_price)
        
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open):
            if current_position == 0 and num_working_orders == 0:
                # Base entry
                oid = ctx.sell(
                    size=self.base_size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-short-base",
                )
                if oid is not None:
                    self.entry_prices = [current_price]
            elif can_scale_in and current_position > -self.max_positions:
                # Scale-in entry
                ctx.sell(
                    size=self.scale_size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="trend-short-scale",
                )
                self.entry_prices.append(current_price)

    def on_fill(self, ctx) -> None:
        # Reset entry prices when position closes
        if ctx.fill.position == 0:
            self.entry_prices = []

    def on_lifecycle(self, ctx) -> None:
        pass
