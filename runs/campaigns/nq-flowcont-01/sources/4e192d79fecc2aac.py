"""
Order flow continuation with dynamic threshold scaling and intra-session regime detection.

Analysis of 38 trials reveals that trial #37 (-0.4820) and #38 (-0.4828) are
marginally better than the baseline #1 (-0.4823), but improvements are minimal.

Key observations:
1. Trial #1's core model (1m + 15m, lookbacks 5/4, brackets 0.8/1.2) is robust
2. Asymmetric thresholds (#37) provide slight improvement
3. Session-aware brackets (#38) did NOT improve over #37 (regressed to -0.4828)
4. All attempts at fine-tuning have either regressed or provided marginal gains

The regression from #37 to #38 suggests that:
- Tighter early-session brackets (0.6/1.0) may be too tight
- The interaction between multiple session-aware parameters is complex
- We may be over-parameterizing the model

New hypothesis: MOMENTUM-BASED REGIME DETECTION + REFINED THRESHOLDS
Instead of time-based early/late session split, detect market regime dynamically:
1. Measure recent price momentum (last 3 bars vs prior 3 bars on slow timeframe)
2. In "quiet" regime (low momentum): use tighter thresholds, smaller size
3. In "trending" regime (high momentum): use wider thresholds, normal size
4. This adapts to actual market conditions, not just clock time

Why this should work:
- Early sessions aren't always quiet; some early sessions trend strongly
- Late sessions aren't always trending; some late sessions are choppy
- Regime detection captures the actual market state
- Momentum-based regimes are forward-looking (not just historical)
- Simpler than time-based: one continuous adaptation, not a binary switch

Implementation approach:
1. Keep trial #37's asymmetric thresholds (buy=0.10, sell=0.14)
2. CHANGE: Replace time-based early/late with momentum-based regimes
   - Calculate slow momentum: compare recent bars to older bars
   - If momentum is low (choppy): buy_threshold=0.12, sell_threshold=0.16, size=1
   - If momentum is high (trending): buy_threshold=0.10, sell_threshold=0.14, size=2
3. Keep brackets fixed at 0.8/1.2 (trial #37 baseline)
4. Keep volatility adaptation for thresholds

Expected outcome:
1. More adaptive to actual market conditions vs. clock time
2. Avoids over-parameterization of #38
3. Should perform between #37 (-0.4820) and #38 (-0.4828)
4. Simpler logic: regime detection is more interpretable than time-based splits
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        momentum_lookback: int = 3,
        momentum_threshold: float = 0.005,
        quiet_buy_threshold: float = 0.12,
        quiet_sell_threshold: float = 0.16,
        trending_buy_threshold: float = 0.10,
        trending_sell_threshold: float = 0.14,
        volatility_window: int = 20,
        quiet_size: int = 1,
        normal_size: int = 2,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            momentum_lookback: Bars to measure momentum over
            momentum_threshold: Threshold for regime detection
            quiet_buy_threshold: Buy threshold in quiet regime
            quiet_sell_threshold: Sell threshold in quiet regime
            trending_buy_threshold: Buy threshold in trending regime
            trending_sell_threshold: Sell threshold in trending regime
            volatility_window: Bars to measure volatility over
            quiet_size: Position size in quiet regime
            normal_size: Position size in trending regime
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.momentum_lookback = momentum_lookback
        self.momentum_threshold = momentum_threshold
        self.quiet_buy_threshold = quiet_buy_threshold
        self.quiet_sell_threshold = quiet_sell_threshold
        self.trending_buy_threshold = trending_buy_threshold
        self.trending_sell_threshold = trending_sell_threshold
        self.volatility_window = volatility_window
        self.quiet_size = quiet_size
        self.normal_size = normal_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        # Skip if we already have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Skip during warmup when orders would be ignored
        if not ctx.actionable:
            return

        # Only act when the fast bar closes
        if self.fast_interval not in ctx.bars:
            return

        fast_history = ctx.history(self.fast_interval)
        slow_history = ctx.history(self.slow_interval)

        # Need enough history for both calculations
        if len(fast_history) < self.lookback_fast:
            return
        if len(slow_history) < self.lookback_slow:
            return
        if len(slow_history) < 2 * self.momentum_lookback:
            return

        # Detect market regime based on momentum
        is_trending = self._detect_trending_regime(slow_history)

        if is_trending:
            size = self.normal_size
            buy_threshold = self.trending_buy_threshold
            sell_threshold = self.trending_sell_threshold
        else:
            size = self.quiet_size
            buy_threshold = self.quiet_buy_threshold
            sell_threshold = self.quiet_sell_threshold

        # Calculate adaptive threshold based on recent volatility
        volatility_lookback = min(self.volatility_window, len(fast_history))
        if volatility_lookback > 0:
            vol_window = fast_history.last(volatility_lookback)
            price_range = max(b.high for b in vol_window) - min(b.low for b in vol_window)
            avg_price = sum(b.close for b in vol_window) / len(vol_window)
            if avg_price > 0:
                volatility_ratio = price_range / avg_price
            else:
                volatility_ratio = 1.0
            # Adapt threshold: higher volatility = higher threshold needed
            volatility_multiplier = 0.8 + 0.4 * volatility_ratio
        else:
            volatility_multiplier = 1.0

        # Get the fast timeframe lookback window for flow measurement
        fast_window = fast_history.last(self.lookback_fast)

        # Calculate signed order flow on fast timeframe
        total_delta = 0.0
        total_volume = 0.0

        for bar in fast_window:
            total_delta += bar.delta
            total_volume += bar.volume

        if total_volume <= 0:
            return

        # Normalize imbalance
        fast_imbalance = total_delta / total_volume

        # Get slow timeframe trend
        slow_window = slow_history.last(self.lookback_slow)
        slow_delta = 0.0
        slow_volume = 0.0

        for bar in slow_window:
            slow_delta += bar.delta
            slow_volume += bar.volume

        if slow_volume <= 0:
            return

        slow_imbalance = slow_delta / slow_volume

        # Entry logic with regime-aware parameters
        buy_threshold_adjusted = buy_threshold * volatility_multiplier
        sell_threshold_adjusted = sell_threshold * volatility_multiplier

        if fast_imbalance > buy_threshold_adjusted and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -sell_threshold_adjusted and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def _detect_trending_regime(self, slow_history) -> bool:
        """
        Detect if market is in a trending regime based on momentum.
        Returns True if trending (high momentum), False if quiet (low momentum).
        """
        # Get two windows: recent and prior
        recent_window = slow_history.last(self.momentum_lookback)
        prior_window = slow_history.last(2 * self.momentum_lookback)[: self.momentum_lookback]

        if len(recent_window) < self.momentum_lookback or len(prior_window) < self.momentum_lookback:
            # Default to trending if not enough data
            return True

        # Calculate average closes
        recent_avg = sum(b.close for b in recent_window) / len(recent_window)
        prior_avg = sum(b.close for b in prior_window) / len(prior_window)

        # Calculate momentum as percentage change
        if prior_avg > 0:
            momentum = abs(recent_avg - prior_avg) / prior_avg
        else:
            momentum = 0.0

        # Trending if momentum exceeds threshold
        return momentum > self.momentum_threshold

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
