"""
Order flow continuation with adaptive multi-regime detection and asymmetric entry logic.

Analysis of 39 trials reveals critical insights:

Trial #1: score -0.4823 (baseline, ~35k trades)
Trial #37: score -0.4820 (asymmetric thresholds, ~36k trades) - BEST
Trial #38: score -0.4828 (session-aware brackets) - REGRESSED
Trial #39: score -0.9408 (momentum-based regime) - SEVERELY REGRESSED

Key finding: Trial #37 remains the best performer. The regression in #39 suggests
that momentum-based regime detection is too unstable or fundamentally misaligned
with the order flow signal.

New hypothesis: STABILITY-FOCUSED REFINEMENT WITH DUAL-THRESHOLD ASYMMETRY
The core insight from trial #37 that works:
1. Asymmetric thresholds (buy=0.10, sell=0.14) capture the directional bias
2. Multi-timeframe confirmation (1m + 15m) filters noise
3. Fixed brackets (0.8/1.2) are stable and proven
4. Session-aware sizing (1 early, 2 late) provides risk management

What failed in attempts to improve:
- Session-aware brackets (#38): Too many parameters, over-optimization
- Momentum regime (#39): Regime detection was too aggressive/unstable
- All other variations: Marginal or negative returns

New approach: MICRO-REFINEMENTS TO TRIAL #37
Instead of adding new regime logic, refine the existing parameters with small,
targeted adjustments:

1. Threshold adjustment: Trial #37 uses fixed 0.10/0.14. Test 0.09/0.13
   - Slightly more aggressive on buys (0.09 vs 0.10)
   - Slightly more selective on sells (0.13 vs 0.14)
   - Rationale: Markets have upside bias; shorts need more confirmation
   - This is a 1-parameter refinement, not a new regime

2. Volatility multiplier tuning: Current formula is 0.8 + 0.4 * vol_ratio
   - Change to 0.85 + 0.35 * vol_ratio (slightly less aggressive scaling)
   - Rationale: Prevent over-adjustment in high volatility periods
   - Keeps the adaptive nature but more conservative

3. Lookback optimization: Trial #37 uses lookback_fast=5, lookback_slow=4
   - Keep these as-is (they're proven)

4. Session sizing: Trial #37 uses 1 contract early, 2 later (60 min threshold)
   - Test 90 minute threshold instead of 60
   - Rationale: Cash open + first half hour of regular session might be noisier
   - Keeps the core sizing strategy, extends the "early" period

Why this approach:
- Trial #37 is the best we have; small refinements are lower risk than new logic
- The asymmetric threshold insight is sound and should be preserved
- Micro-adjustments to parameters are less likely to introduce instability
- This maintains the simplicity that made #37 work
- Expected improvement: Small but consistent vs. the regression risk of new logic

Expected outcome: Score between -0.4810 and -0.4825 (slight improvement or stability)
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        buy_threshold: float = 0.09,
        sell_threshold: float = 0.13,
        volatility_window: int = 20,
        early_session_minutes: int = 90,
        early_session_size: int = 1,
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
            buy_threshold: Imbalance threshold for buy signals (lower = more longs)
            sell_threshold: Imbalance threshold for sell signals (higher = more selective)
            volatility_window: Bars to measure volatility over
            early_session_minutes: Minutes from session open to use reduced size
            early_session_size: Position size during early session
            normal_size: Position size after early session
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.volatility_window = volatility_window
        self.early_session_minutes = early_session_minutes
        self.early_session_size = early_session_size
        self.normal_size = normal_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self._session = None
        self._session_start_ts = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        # Track session changes
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_start_ts = ctx.session_start

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

        # Determine position size based on session time
        minutes_into_session = (ctx.ts - self._session_start_ts) / (60 * 1_000_000_000)
        if minutes_into_session < self.early_session_minutes:
            size = self.early_session_size
        else:
            size = self.normal_size

        # Calculate adaptive threshold based on recent volatility
        # Refined formula: 0.85 + 0.35 * vol_ratio (less aggressive scaling)
        volatility_lookback = min(self.volatility_window, len(fast_history))
        if volatility_lookback > 0:
            vol_window = fast_history.last(volatility_lookback)
            price_range = max(b.high for b in vol_window) - min(b.low for b in vol_window)
            avg_price = sum(b.close for b in vol_window) / len(vol_window)
            if avg_price > 0:
                volatility_ratio = price_range / avg_price
            else:
                volatility_ratio = 1.0
            # Refined volatility multiplier: more conservative scaling
            volatility_multiplier = 0.85 + 0.35 * volatility_ratio
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

        # Entry logic with asymmetric thresholds:
        # - Buy threshold is lower (0.09) to capture upside flow more readily
        # - Sell threshold is higher (0.13) to be more selective on shorts
        # - Slow imbalance provides directional confirmation
        # - Position size varies by session time (smaller early, normal later)

        buy_threshold_adjusted = self.buy_threshold * volatility_multiplier
        sell_threshold_adjusted = self.sell_threshold * volatility_multiplier

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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
