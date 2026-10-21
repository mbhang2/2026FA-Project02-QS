"""
Analysis of trials #30-#33 and next optimization direction:

All four recent trials (#30-#33) scored +1.3264 to +1.3269 with 645-653 trades,
indicating we've reached a plateau in the current parameter space. The regime-aware
filtering in #33 didn't improve over the baseline, suggesting volatility regime
alone isn't the right lever.

Key insight: Looking at the trial history, the best performers consistently have:
- 645-653 trades (optimal frequency)
- Fast MA (6-7) + Slow MA (19-20) for trend
- Asymmetric vol scaling (1.14/1.45)
- Bar strength threshold around 0.36-0.39

For trial #34, I'll test a fundamentally different hypothesis: the issue may be
entry timing relative to the bar close. Currently, we enter based on the last
closed bar. What if we add a momentum filter based on the rate of change (ROC)
of price relative to the MAs?

New approach:
1. Keep the proven MA trend core (fast=6, slow=20)
2. Add a momentum filter: only enter if price is accelerating toward the MA
3. Use ROC (rate of change) over 2-3 bars to detect momentum
4. This should reduce whipsaws on reversals while maintaining trade frequency

Additionally:
- vol_lookback=18 (proven)
- Asymmetric scaling (1.14/1.45) unchanged
- bar_strength_threshold=0.38 (proven)
- price_tolerance=0.0015 (proven)
- NEW: momentum_lookback=3, min_momentum=0.0002 for acceleration filter

This targets entry quality by filtering for directional momentum, not just
directional bars, which should improve win rate.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.14,
                 vol_multiplier_target: float = 1.45,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.95, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.38,
                 price_tolerance: float = 0.0015,
                 momentum_lookback: int = 3,
                 min_momentum: float = 0.0002,
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
        self.momentum_lookback = momentum_lookback
        self.min_momentum = min_momentum
        self.size = size
        # Accumulate closes for MA and volatility calculations
        self.closes: list[float] = []
        # Track returns for volatility
        self.returns: list[float] = []

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
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + self.momentum_lookback + 5
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
        
        # Calculate momentum (rate of change over recent bars)
        momentum_up = 0.0
        momentum_down = 0.0
        if len(self.closes) >= self.momentum_lookback + 1:
            momentum_closes = self.closes[-(self.momentum_lookback + 1):]
            first_price = momentum_closes[0]
            last_price = momentum_closes[-1]
            if first_price > 0:
                momentum_change = (last_price - first_price) / first_price
                if momentum_change > 0:
                    momentum_up = momentum_change
                else:
                    momentum_down = -momentum_change
        
        # Use asymmetric volatility scaling for stop and target
        vol_adjustment_stop = 1.0 + (vol * self.vol_multiplier_stop)
        vol_adjustment_target = 1.0 + (vol * self.vol_multiplier_target)
        
        stop_offset = max(self.min_stop, min(self.max_stop, self.min_stop * vol_adjustment_stop))
        target_offset = max(self.min_target, min(self.max_target, self.min_target * vol_adjustment_target))
        
        # Price tolerance band to reduce MA crossover whipsaws
        tolerance_band = self.price_tolerance * slow_ma
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up + upward momentum
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open and momentum_up >= self.min_momentum):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down + downward momentum
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open and momentum_down >= self.min_momentum):
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
