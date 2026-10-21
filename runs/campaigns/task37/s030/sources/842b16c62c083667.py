"""
Analysis of trials #30-#40 and the optimization plateau:

Best score: #40 at +1.4150 with 660 trades (improvement over #30's +1.3269).

Key observations from the trial sequence:
1. Relaxing filters (trial #40: bar_strength 0.39→0.35, price_tolerance 0.0015→0.0020) 
   improved score from +1.3269 to +1.4150
2. This confirms the hypothesis: synthetic data rewards *consistency* over *selectivity*
3. Trade count increased from 653 to 660, still within the optimal 645-720 range
4. Removing momentum filter (vs #39) was correct - it was over-filtering

Current bottleneck analysis for trial #41:
- We're at +1.4150, a local optimum with the current parameter set
- Further relaxation of bar_strength or price_tolerance might yield diminishing returns
- But we haven't optimized the *bracket scaling* in concert with the relaxed entry filters

New hypothesis:
When we relax entry filters (accepting weaker bars, wider price bands), we need to 
*tighten* the bracket to compensate. Currently:
- vol_multiplier_stop = 1.14 (stop = 0.70 * (1 + vol*1.14))
- vol_multiplier_target = 1.45 (target = 0.95 * (1 + vol*1.45))

The issue: with more frequent entries on weaker bars, we're giving away too much 
to the target. We should:
1. Keep the relaxed entry filters (bar_strength=0.35, price_tolerance=0.0020)
2. **Reduce vol_multiplier_target from 1.45 to 1.25** (tighter target)
3. **Slightly increase vol_multiplier_stop from 1.14 to 1.20** (better risk/reward)
4. **Reduce min_target from 0.95 to 0.85** (absolute target tighter)

This maintains high trade frequency but improves win rate by:
- Taking profits faster (tighter targets)
- Protecting downside better (slightly wider stops relative to target)
- Better risk/reward on each trade

The bracket should scale proportionally: if entries are less selective, exits 
should be more aggressive about locking in gains.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 6,
                 slow_lookback: int = 20, vol_lookback: int = 18,
                 vol_multiplier_stop: float = 1.20,
                 vol_multiplier_target: float = 1.25,
                 min_stop: float = 0.70, max_stop: float = 1.5,
                 min_target: float = 0.85, max_target: float = 2.85,
                 min_ma_divergence: float = 0.00144,
                 bar_strength_threshold: float = 0.35,
                 price_tolerance: float = 0.0020,
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
        
        # Buy: uptrend (fast > slow) + price >= fast MA - tolerance + bar closed up
        if (fast_ma > slow_ma and current_price >= (fast_ma - tolerance_band) and 
            last_bar.close > last_bar.open):
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="trend-long",
            )
        # Sell: downtrend (fast < slow) + price <= fast MA + tolerance + bar closed down
        elif (fast_ma < slow_ma and current_price <= (fast_ma + tolerance_band) and 
              last_bar.close < last_bar.open):
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
