"""
Order flow continuation with regime-aware entry filtering and cost optimization.

Analysis of 28 trials reveals a critical pattern:
- Trial #1 (best): score -0.4823, ~35k trades
  - Core signal: adaptive volatility threshold + multi-timeframe confirmation
  - Brackets: stop_offset=0.8, target_offset=1.2 (tight, 1:1.5 risk:reward)
- Trial #27: score -0.9554, ~102k trades (wider brackets made it worse)
- Trial #28: score -0.9665, ~174k trades (flow momentum + volume filter degraded)

Key insight from trial #1's success:
1. The adaptive volatility threshold is the core strength
2. Multi-timeframe confirmation (1m + 15m) works well
3. The tight brackets (0.8/1.2) are actually appropriate for this signal

Why subsequent trials failed:
- Trial #27: Wider brackets (1.0/1.5) increased stop-outs on normal volatility
- Trial #28: Flow momentum captured too many marginal signals
- All trials #2-28: Adding complexity to a working signal degraded results

The real problem: We're entering TOO FREQUENTLY on weak signals.
Trial #1 had ~35k trades. Trial #28 had ~174k. More trades = more commission.

New hypothesis: The issue is REGIME SELECTIVITY, not signal mechanics.
- Trade only when conditions favor continuation (low volatility regimes)
- Avoid trading during high-volatility regimes where flow fades quickly
- Use the session's opening volatility as a regime filter
- Skip trading entirely during the first 30 minutes (opening noise)
- Skip trading during the last hour (closing chaos and overnight gap risk)

Strategy:
1. Keep trial #1's exact signal logic (adaptive threshold + multi-timeframe)
2. Keep trial #1's brackets (stop=0.8, target=1.2)
3. Add session timing filters: skip first 30min and last 60min of session
4. Add volatility regime filter: only trade if current volatility < session avg
5. This reduces trade frequency (back toward 35k) while keeping good signals
6. Expected: Higher win rate per trade = better net P&L despite fewer trades

The logic: Trial #1 works. We should make it MORE selective, not less.
By filtering out high-volatility regimes and session edges, we trade only
the best-quality signals, improving win rate and reducing commission drag.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.12,
        volatility_window: int = 20,
        size: int = 1,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
        min_session_minutes: int = 30,
        max_session_minutes: int = 60,
        volatility_regime_percentile: float = 0.6,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Base imbalance threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            size: Position size
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
            min_session_minutes: Skip trading in first N minutes of session
            max_session_minutes: Skip trading in last N minutes of session
            volatility_regime_percentile: Only trade if volatility below this percentile
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.min_session_minutes = min_session_minutes
        self.max_session_minutes = max_session_minutes
        self.volatility_regime_percentile = volatility_regime_percentile
        
        # Track session volatilities for regime detection
        self.session_volatilities: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def _is_in_trading_window(self, ctx) -> bool:
        """Check if we're in the tradeable part of the session."""
        time_since_open = ctx.ts - ctx.session_start
        time_until_close = ctx.session_end - ctx.ts
        
        # Skip first N minutes of session (opening noise)
        if time_since_open < self.min_session_minutes * 60 * 1_000_000_000:
            return False
        
        # Skip last N minutes of session (closing chaos, overnight gap risk)
        if time_until_close < self.max_session_minutes * 60 * 1_000_000_000:
            return False
        
        return True

    def _get_volatility_regime_threshold(self) -> float:
        """Get the volatility threshold for regime filtering."""
        if not self.session_volatilities:
            return float('inf')  # No history yet, allow all
        
        # Sort and get the percentile
        sorted_vols = sorted(self.session_volatilities)
        idx = int(len(sorted_vols) * self.volatility_regime_percentile)
        idx = min(idx, len(sorted_vols) - 1)
        return sorted_vols[idx]

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

        # Check if we're in the tradeable window
        if not self._is_in_trading_window(ctx):
            return

        fast_history = ctx.history(self.fast_interval)
        slow_history = ctx.history(self.slow_interval)

        # Need enough history for both calculations
        if len(fast_history) < self.lookback_fast:
            return
        if len(slow_history) < self.lookback_slow:
            return

        # Calculate current volatility (range over window)
        volatility_lookback = min(self.volatility_window, len(fast_history))
        if volatility_lookback > 0:
            vol_window = fast_history.last(volatility_lookback)
            price_range = max(b.high for b in vol_window) - min(b.low for b in vol_window)
            avg_price = sum(b.close for b in vol_window) / len(vol_window)
            if avg_price > 0:
                current_volatility = price_range / avg_price
            else:
                current_volatility = 0.0
            
            # Track this volatility for regime detection
            self.session_volatilities.append(current_volatility)
            # Keep only recent history to avoid stale data
            if len(self.session_volatilities) > 200:
                self.session_volatilities = self.session_volatilities[-200:]
            
            # Adapt threshold: higher volatility = higher threshold needed
            threshold = self.base_threshold * (0.8 + 0.4 * current_volatility)
        else:
            threshold = self.base_threshold
            current_volatility = 0.0

        # VOLATILITY REGIME FILTER: Only trade in low-volatility regimes
        # This filters out the high-volatility periods where flow fades quickly
        regime_threshold = self._get_volatility_regime_threshold()
        if current_volatility > regime_threshold:
            return

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

        # Entry logic (trial #1 core, with regime filtering):
        # - Fast imbalance must exceed threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - Current volatility must be in favorable regime
        # - Must be in tradeable window of session

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # Reset session volatilities on new session
        if ctx.event.kind == "session_open":
            self.session_volatilities = []
