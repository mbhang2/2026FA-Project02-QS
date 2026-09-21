"""
Order flow continuation with session-phase adaptive entry and exit management.

Analysis of prior trials reveals a critical pattern:
- Trial #1 (best): score -0.4823, ~35k trades, adaptive threshold + multi-timeframe
- Trial #18: score -0.8289, added trade frequency limiting
- Most refinements made things worse

Key insight: Trial #1 worked well because it balanced signal frequency with quality.
The problem with subsequent trials: they either over-engineered the signal or under-traded it.

New approach - focus on WHEN we trade within the session:
1. Keep trial #1's core logic (adaptive volatility threshold + multi-timeframe confirmation)
2. Add session-phase awareness: Order flow behavior differs across the session
   - Early session (first 2 hours): Often trending, flow continuation stronger
   - Mid session: More mean-reversion, flow less reliable
   - Late session: Mixed, but we want to reduce late-session exposure
3. Use dynamic entry thresholds based on session progress
4. Add intelligent exit management: Don't just use fixed brackets
   - Scale target_offset based on how long we've held the position
   - Tighten stops as we approach session end to reduce overnight risk
5. Reduce position size to 1 (trial #1 used size=2 but size=1 is more robust)
6. Keep the adaptive volatility logic from trial #1 (it worked)
7. Simplify slow_imbalance confirmation: just require same direction (like trial #1)

The hypothesis: Trial #1 was good because it traded frequently with a real signal.
The edge exists but is small. By trading at optimal times in the session (when flow
continuation is strongest) and managing exits better, we can improve the win rate
without reducing trade frequency too much.

Session phasing insight: The first 2 hours of the session (pre-cash-open for NQ)
typically show stronger continuation patterns. Trading more aggressively then and
more conservatively later should improve the score.
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
        self.entry_price = None
        self.entry_time = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def _get_session_phase(self, ctx) -> float:
        """
        Return session progress as a fraction [0.0, 1.0].
        0.0 = session start, 1.0 = session end.
        """
        if ctx.session_end <= ctx.session_start:
            return 0.5
        return (ctx.ts - ctx.session_start) / (ctx.session_end - ctx.session_start)

    def _get_phase_adjusted_threshold(self, base_threshold: float, phase: float) -> float:
        """
        Adjust threshold based on session phase.
        Early session (0-0.3): Lower threshold (more aggressive)
        Mid session (0.3-0.7): Normal threshold
        Late session (0.7-1.0): Higher threshold (more conservative)
        """
        if phase < 0.3:
            # Early session: reduce threshold by 15%
            return base_threshold * 0.85
        elif phase < 0.7:
            # Mid session: keep normal
            return base_threshold
        else:
            # Late session: increase threshold by 10%
            return base_threshold * 1.10

    def _get_phase_adjusted_target(self, base_target: float, phase: float) -> float:
        """
        Adjust target offset based on session phase.
        Early session: Use full target (more time to run)
        Late session: Reduce target (less time, want to lock in gains)
        """
        if phase < 0.3:
            return base_target
        elif phase < 0.7:
            return base_target * 0.95
        else:
            # Late session: reduce target to 80% of base
            return base_target * 0.80

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
            base_threshold = self.base_threshold * (0.8 + 0.4 * volatility_ratio)
        else:
            base_threshold = self.base_threshold

        # Get session phase and adjust threshold
        phase = self._get_session_phase(ctx)
        threshold = self._get_phase_adjusted_threshold(base_threshold, phase)
        target_offset = self._get_phase_adjusted_target(self.target_offset, phase)

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

        # Entry logic (same as trial #1, but with phase-adjusted parameters):
        # - Fast imbalance must exceed phase-adjusted threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - Use phase-adjusted target offset

        if fast_imbalance > threshold and slow_imbalance > 0:
            # Strong buying pressure on fast, confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=target_offset,
                tag="flow-long",
            )
            self.entry_price = ctx.last_price
            self.entry_time = ctx.ts
        elif fast_imbalance < -threshold and slow_imbalance < 0:
            # Strong selling pressure on fast, confirmed by slow
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=target_offset,
                tag="flow-short",
            )
            self.entry_price = ctx.last_price
            self.entry_time = ctx.ts

    def on_fill(self, ctx) -> None:
        # Reset entry tracking on fill
        if ctx.fill.position == 0:
            self.entry_price = None
            self.entry_time = None

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
