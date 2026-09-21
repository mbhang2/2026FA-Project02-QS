"""
Order flow continuation with dual-threshold asymmetric entry and session-aware filtering.

Analysis of 36 trials reveals a critical pattern:

Trial #1 (best): score -0.4823, ~35k trades
- Adaptive volatility threshold (0.12) + multi-timeframe (1m + 15m)
- Brackets (stop=0.8, target=1.2)
- Lookbacks: fast=5, slow=4
- Size=2, symmetric thresholds

Trial #36 (recent): score -0.4881, ~27k trades
- Exponential weighting on fast imbalance
- SLIGHTLY WORSE than #1

Key insight from all 36 trials: Trial #1 remains best. All modifications
(higher threshold, exponential weighting, tighter brackets) have made it worse.

This suggests the core model is sound, but we're over-optimizing.

New hypothesis: ASYMMETRIC THRESHOLDS + SESSION CONTEXT
- Buy signals (upside) might require different sensitivity than sell signals
- Order flow might have different predictive power at different times of day
- The cash open (first hour) has different dynamics than the rest of the session
- Morning session might have stronger flow continuation than overnight

Strategy: ASYMMETRIC ENTRY THRESHOLDS
1. Keep trial #1's core structure (1m + 15m, lookbacks 5/4, brackets 0.8/1.2)
2. CHANGE: Use asymmetric thresholds
   - Buy threshold: 0.10 (lower, easier to enter longs)
   - Sell threshold: 0.14 (higher, more selective on shorts)
   - Rationale: Markets might have upside bias; shorts might need stronger signals
3. CHANGE: Add session-aware filtering
   - Track which session we're in via ctx.session
   - Reduce position size in early session (first 60 minutes) to 1 contract
   - Use full size (2) for rest of session
   - Rationale: Early session is noisier, full size after market settles
4. Keep everything else from trial #1

Why asymmetry matters:
- Bull markets reward longs more than shorts
- Upside flow might be more persistent than downside flow
- This is a real market phenomenon, not over-fitting
- Asymmetric thresholds are a single parameter change (buy_threshold, sell_threshold)

Why session awareness matters:
- Cash open (first 60 min) has different order flow characteristics
- Overnight session has different participation
- Reducing size early = less exposure to noisy flow
- Full size later = capture cleaner signals
- Session index is always available, no lookahead

Expected outcome: Better risk-adjusted returns by:
1. Entering more longs (lower buy threshold) when flow is bullish
2. Being more selective on shorts (higher sell threshold)
3. Reducing early-session noise exposure with smaller size
4. Capturing cleaner mid/late-session signals with full size
5. Net: Better P&L than trial #1's -0.4823
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        buy_threshold: float = 0.10,
        sell_threshold: float = 0.14,
        volatility_window: int = 20,
        early_session_minutes: int = 60,
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

        # Entry logic with asymmetric thresholds:
        # - Buy threshold is lower (0.10) to capture upside flow more readily
        # - Sell threshold is higher (0.14) to be more selective on shorts
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
