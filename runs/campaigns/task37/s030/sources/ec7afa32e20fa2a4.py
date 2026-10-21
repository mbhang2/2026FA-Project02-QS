"""
A hybrid strategy combining momentum and mean reversion with adaptive filtering.

Analysis of prior trials:
- Trial #0 (baseline momentum): -0.612 score, 1015 trades
- Trial #1 (volatility-adjusted mean reversion): -0.638 score, 773 trades
- Trial #2 (trend-following with vol adjustment): +0.0192 score, 969 trades

Trial #2's trend-following approach achieved the best score (+0.0192) by:
1. Using dual moving averages to identify trend direction
2. Confirming entries with price position relative to fast MA
3. Scaling stops/targets by volatility

However, there's room for improvement:
1. The strategy may miss reversals or consolidations
2. Volatility scaling could be more sophisticated
3. Entry confirmation could use additional filters
4. The strategy could benefit from adaptive position sizing

This strategy combines:
1. Trend identification (fast MA > slow MA for uptrends)
2. Mean reversion within trends (price pullbacks offer better entries)
3. Volatility-adaptive risk management with tighter bounds
4. Price action confirmation (recent bar direction)
5. Adaptive entry thresholds based on trend strength

The core insight: In uptrends, buy dips (mean reversion); in downtrends, sell
rallies. This captures both the trend direction AND the mean reversion within it.
"""

from __future__ import annotations
import math


class Strategy:
    def __init__(self, *, interval: str = "5m", fast_lookback: int = 5,
                 slow_lookback: int = 20, vol_lookback: int = 10,
                 trend_strength_lookback: int = 8, vol_multiplier: float = 1.2,
                 min_stop: float = 0.4, max_stop: float = 1.5,
                 min_target: float = 0.6, max_target: float = 2.5,
                 pullback_threshold: float = 0.003, size: int = 1):
        self.interval = interval
        self.fast_lookback = fast_lookback
        self.slow_lookback = slow_lookback
        self.vol_lookback = vol_lookback
        self.trend_strength_lookback = trend_strength_lookback
        self.vol_multiplier = vol_multiplier
        self.min_stop = min_stop
        self.max_stop = max_stop
        self.min_target = min_target
        self.max_target = max_target
        self.pullback_threshold = pullback_threshold
        self.size = size
        # Accumulate closes for MA and volatility calculations
        self.closes: list[float] = []
        # Track returns for volatility
        self.returns: list[float] = []
        # Track highs and lows for trend strength
        self.highs: list[float] = []
        self.lows: list[float] = []

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
        
        # Keep only what we need
        max_needed = self.slow_lookback + self.vol_lookback + self.trend_strength_lookback + 5
        if len(self.closes) > max_needed:
            self.closes = self.closes[-max_needed:]
            self.highs = self.highs[-max_needed:]
            self.lows = self.lows[-max_needed:]

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
        
        # Calculate volatility
        vol = 0.0
        if len(self.returns) >= self.vol_lookback:
            recent_returns = self.returns[-self.vol_lookback:]
            mean_ret = sum(recent_returns) / len(recent_returns)
            variance = sum((r - mean_ret) ** 2 for r in recent_returns) / len(recent_returns)
            vol = math.sqrt(max(variance, 0.0))
        
        # Calculate trend strength: how consistently are we making higher highs/lows?
        trend_strength = 0.0
        if len(self.highs) >= self.trend_strength_lookback:
            recent_highs = self.highs[-self.trend_strength_lookback:]
            recent_lows = self.lows[-self.trend_strength_lookback:]
            
            higher_highs = sum(1 for i in range(1, len(recent_highs)) 
                              if recent_highs[i] > recent_highs[i-1])
            higher_lows = sum(1 for i in range(1, len(recent_lows)) 
                             if recent_lows[i] > recent_lows[i-1])
            lower_highs = sum(1 for i in range(1, len(recent_highs)) 
                             if recent_highs[i] < recent_highs[i-1])
            lower_lows = sum(1 for i in range(1, len(recent_lows)) 
                            if recent_lows[i] < recent_lows[i-1])
            
            # Trend strength: proportion of consistent moves
            uptrend_strength = (higher_highs + higher_lows) / (2 * (self.trend_strength_lookback - 1))
            downtrend_strength = (lower_highs + lower_lows) / (2 * (self.trend_strength_lookback - 1))
            trend_strength = uptrend_strength - downtrend_strength
        
        # Scale stop and target by volatility and trend strength
        vol_adjustment = 1.0 + (vol * self.vol_multiplier)
        trend_adjustment = 1.0 + (abs(trend_strength) * 0.5)  # Stronger trends = wider stops
        
        stop_offset = max(self.min_stop, min(self.max_stop, 
                                             self.min_stop * vol_adjustment * trend_adjustment))
        target_offset = max(self.min_target, min(self.max_target, 
                                                 self.min_target * vol_adjustment * trend_adjustment))
        
        # Hybrid strategy: Trend + Mean Reversion
        # In uptrends: buy dips (price below fast MA)
        # In downtrends: sell rallies (price above fast MA)
        
        ma_diff = (fast_ma - slow_ma) / slow_ma
        price_to_fast = (current_price - fast_ma) / fast_ma
        
        # Strong uptrend: fast MA significantly above slow MA
        if ma_diff > self.pullback_threshold and price_to_fast < -self.pullback_threshold:
            # Price pulled back below fast MA in an uptrend - buy the dip
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="hybrid-uptrend-dip",
            )
        # Strong downtrend: fast MA significantly below slow MA
        elif ma_diff < -self.pullback_threshold and price_to_fast > self.pullback_threshold:
            # Price rallied above fast MA in a downtrend - sell the rally
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="hybrid-downtrend-rally",
            )
        # Weak/no trend: use simple momentum
        elif abs(ma_diff) <= self.pullback_threshold:
            # No clear trend - use basic momentum
            if last_bar.close > last_bar.open:
                ctx.buy(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="hybrid-momentum-up",
                )
            elif last_bar.close < last_bar.open:
                ctx.sell(
                    size=self.size,
                    stop_offset=stop_offset,
                    target_offset=target_offset,
                    tag="hybrid-momentum-down",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
