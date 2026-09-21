"""
Order flow continuation with refined early-session dynamics and volatility-adaptive brackets.

Analysis of 37 trials reveals key patterns:

Trial #1: score -0.4823, ~35k trades (baseline)
- Adaptive volatility threshold (0.12)
- Multi-timeframe (1m + 15m)
- Brackets (stop=0.8, target=1.2)
- Lookbacks: fast=5, slow=4
- Size=2

Trial #37: score -0.4820, ~36k trades
- Asymmetric thresholds (buy=0.10, sell=0.14)
- Session-aware sizing (1 contract early, 2 later)
- Slightly better than #1

Key insight: Trial #37 is marginally better than #1 (-0.4820 vs -0.4823),
suggesting that the session-aware sizing approach has merit. However, the
improvement is small, and we haven't fully optimized the interaction between:
1. Early-session dynamics (first 60 minutes)
2. Volatility-adaptive brackets
3. The asymmetric thresholds

New hypothesis: VOLATILITY-ADAPTIVE BRACKETS + REFINED EARLY-SESSION LOGIC
- Trial #37 uses fixed brackets (0.8/1.2), but volatility changes throughout the session
- Early session is noisier—tighter brackets (stop=0.6, target=1.0) make sense
- Later session is smoother—wider brackets (stop=0.8, target=1.2) capture larger moves
- This adapts risk management to actual market conditions

Additional refinement: EARLY-SESSION THRESHOLD ADJUSTMENT
- Early session (first 60 min) might need slightly different thresholds
- Reduce buy_threshold to 0.09 (even more aggressive on longs early)
- Keep sell_threshold at 0.14 (conservative on shorts early)
- Later session: revert to buy=0.10, sell=0.14

Why this matters:
1. Early session has different participation (fewer participants, noisier)
2. Tighter brackets reduce exposure to early-session whipsaws
3. Later session has cleaner flow signals, wider brackets capture bigger moves
4. Volatility-adaptive brackets are proven in risk management
5. This is a natural extension of trial #37's session-aware approach

Implementation:
1. Keep trial #37's core (asymmetric thresholds, session awareness)
2. CHANGE: Make brackets session-aware
   - Early session: stop=0.6, target=1.0 (tighter risk/reward)
   - Later session: stop=0.8, target=1.2 (wider risk/reward)
3. CHANGE: Early-session thresholds slightly more aggressive
   - Early: buy=0.09, sell=0.14
   - Later: buy=0.10, sell=0.14
4. Keep everything else from trial #37

Expected outcome: Better risk-adjusted returns by:
1. Tighter brackets early → fewer whipsaws, better win rate early
2. Wider brackets later → capture cleaner, bigger moves
3. More aggressive buy entry early → capture morning rallies
4. Consistent sell discipline → avoid early-session short traps
5. Net: Better P&L than trial #37's -0.4820 and trial #1's -0.4823
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        early_buy_threshold: float = 0.09,
        early_sell_threshold: float = 0.14,
        late_buy_threshold: float = 0.10,
        late_sell_threshold: float = 0.14,
        volatility_window: int = 20,
        early_session_minutes: int = 60,
        early_session_size: int = 1,
        normal_size: int = 2,
        early_stop_offset: float = 0.6,
        early_target_offset: float = 1.0,
        late_stop_offset: float = 0.8,
        late_target_offset: float = 1.2,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            early_buy_threshold: Buy threshold during early session
            early_sell_threshold: Sell threshold during early session
            late_buy_threshold: Buy threshold after early session
            late_sell_threshold: Sell threshold after early session
            volatility_window: Bars to measure volatility over
            early_session_minutes: Minutes from session open for early session
            early_session_size: Position size during early session
            normal_size: Position size after early session
            early_stop_offset: Stop loss distance during early session
            early_target_offset: Profit target distance during early session
            late_stop_offset: Stop loss distance after early session
            late_target_offset: Profit target distance after early session
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.early_buy_threshold = early_buy_threshold
        self.early_sell_threshold = early_sell_threshold
        self.late_buy_threshold = late_buy_threshold
        self.late_sell_threshold = late_sell_threshold
        self.volatility_window = volatility_window
        self.early_session_minutes = early_session_minutes
        self.early_session_size = early_session_size
        self.normal_size = normal_size
        self.early_stop_offset = early_stop_offset
        self.early_target_offset = early_target_offset
        self.late_stop_offset = late_stop_offset
        self.late_target_offset = late_target_offset
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

        # Determine session phase and corresponding parameters
        minutes_into_session = (ctx.ts - self._session_start_ts) / (60 * 1_000_000_000)
        is_early_session = minutes_into_session < self.early_session_minutes

        if is_early_session:
            size = self.early_session_size
            buy_threshold = self.early_buy_threshold
            sell_threshold = self.early_sell_threshold
            stop_offset = self.early_stop_offset
            target_offset = self.early_target_offset
        else:
            size = self.normal_size
            buy_threshold = self.late_buy_threshold
            sell_threshold = self.late_sell_threshold
            stop_offset = self.late_stop_offset
            target_offset = self.late_target_offset

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

        # Entry logic with session-aware parameters:
        # - Thresholds and brackets vary by session phase
        # - Early session: tighter brackets, more aggressive buys
        # - Late session: wider brackets, standard thresholds
        # - Volatility adjustment applied to thresholds

        buy_threshold_adjusted = buy_threshold * volatility_multiplier
        sell_threshold_adjusted = sell_threshold * volatility_multiplier

        if fast_imbalance > buy_threshold_adjusted and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -sell_threshold_adjusted and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
