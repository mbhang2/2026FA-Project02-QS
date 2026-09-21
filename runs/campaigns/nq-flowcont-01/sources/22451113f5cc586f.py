"""
Order flow continuation with session-aware adaptive exits.

Analysis of trial history shows trial #1 (score -0.4823) remains the best by far.
Every subsequent attempt to improve it has degraded performance:
- Trial #23 (signal strength scaling): -0.9578 (much worse)
- Trial #24 (mean reversion trailing): -0.9702 (much worse)
- Trial #22 (session timing): -0.8647 (worse)

The pattern is clear: the core signal from trial #1 is correct, but we keep making
the exits worse. Trial #1 traded ~35k times with -0.4823 score. The negative score
indicates net losses, but this is the best signal we've found.

Key insight: Commission is 0.1 points per side (0.2 round-trip), and with ~35k trades
that's 7,000 points of commission cost. Our gross P&L must be fighting against this.

New approach - REDUCE TRADING FREQUENCY while keeping the good signal:
Trial #1's parameters work well at the signal level. The problem is we're trading too
much and paying too much commission. Instead of trying to improve exits, let's:

1. Keep trial #1's core completely (adaptive threshold + multi-timeframe confirmation)
2. Add a FLOW MOMENTUM FILTER: only trade when flow is accelerating
   - Measure the rate of change of imbalance across bars
   - Only enter when the current bar's imbalance is stronger than the previous bar
   - This filters out the weakest signals without changing the core logic
3. Keep size=1 and brackets from trial #1 (stop_offset=0.8, target_offset=1.2)
4. This should reduce trade count from 35k to ~20-25k while keeping the best trades
5. Fewer trades = less commission = better net P&L

The hypothesis: Trial #1 makes ~35k trades with -0.4823 score. If we can reduce
to 20k trades by filtering out the weakest signals, and those 20k trades have the
same win rate, we'd save ~3,000 points of commission, which could swing the score
from -0.4823 to something like -0.25 to -0.30.

This is not changing the signal - it's filtering to trade only the highest-quality
instances of the signal we already know works.
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
        momentum_bars: int = 2,
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
            momentum_bars: Bars to measure flow momentum over
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
        self.momentum_bars = momentum_bars

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
            threshold = self.base_threshold * (0.8 + 0.4 * volatility_ratio)
        else:
            threshold = self.base_threshold

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

        # NEW: Check for flow momentum - only trade when imbalance is accelerating
        # This filters out weak signals and reduces trade frequency
        momentum_lookback = min(self.momentum_bars + 1, len(fast_history))
        if momentum_lookback >= self.momentum_bars + 1:
            momentum_window = fast_history.last(self.momentum_bars + 1)
            
            # Calculate imbalance for each bar in momentum window
            imbalances = []
            for bar in momentum_window:
                if bar.volume > 0:
                    imbalances.append(bar.delta / bar.volume)
                else:
                    imbalances.append(0.0)
            
            # Check if current imbalance is stronger than previous bars
            # (i.e., momentum is in the direction we want to trade)
            current_imb = imbalances[-1]
            prev_imb = imbalances[-2]
            
            # For long: current imbalance must be more positive than previous
            # For short: current imbalance must be more negative than previous
            has_momentum_long = current_imb > prev_imb and current_imb > 0
            has_momentum_short = current_imb < prev_imb and current_imb < 0
        else:
            has_momentum_long = False
            has_momentum_short = False

        # Entry logic (trial #1 base, with momentum filter):
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction (confirming trend)
        # - Current imbalance must be stronger than previous bar (momentum)

        if fast_imbalance > threshold and slow_imbalance > 0 and has_momentum_long:
            # Strong buying pressure on fast, confirmed by slow, with momentum
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < 0 and has_momentum_short:
            # Strong selling pressure on fast, confirmed by slow, with momentum
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
        # No special lifecycle handling needed
        pass
