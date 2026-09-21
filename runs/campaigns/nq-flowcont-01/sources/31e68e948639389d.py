"""
Order flow continuation with session-time filtering and optimal entry timing.

Analysis of trial history:
- Trial #1 (best): score -0.4823, ~35k trades - adaptive threshold + multi-timeframe
- Trial #21: score -0.8647, ~31k trades - raised threshold to 0.18, required slow confirmation
- Trial #20: score -0.8909, ~34k trades - added reversal/volume filters (made it worse)
- Trial #19: score -0.9599, ~106k trades - session phasing made it worse

Key observation: Trial #1's superiority suggests the signal is real but timing matters.
The adaptive threshold and multi-timeframe confirmation work. But we're still losing money.

New hypothesis - SESSION TIMING MATTERS:
1. Order flow continuation may work better at certain times of day
2. The cash open (first 30-60 minutes) has different dynamics than the rest of the session
3. The close period (last hour) is also different - more noise, more reversals
4. Mid-session (2-4 hours in) may have the cleanest flow signals

Approach:
1. Keep trial #1's core: adaptive volatility threshold (0.12) + multi-timeframe confirmation
2. Add SESSION TIMING FILTER:
   - Skip entries in first 45 minutes of session (cash open chaos)
   - Skip entries in last 90 minutes of session (close period noise)
   - Only trade in the "sweet spot" middle hours where flow is cleanest
3. Keep lookback_fast=5, lookback_slow=4 (trial #1 parameters)
4. Keep size=1, stop_offset=0.8, target_offset=1.2 (trial #1 parameters)
5. Keep base_threshold=0.12 (trial #1 value)
6. Require slow_imbalance > 0.02 (light confirmation, not too strict)

The logic: Trial #1 made 35k trades and scored -0.4823. If session timing is real,
then filtering out the worst 20-25% of trades (open and close periods) while keeping
the best 75-80% could improve score significantly. We're not adding complexity,
just filtering by time-of-day.

Sessions run 17:00 CT previous day to 16:00 CT current day (~23 hours).
- First 45 min: skip (chaotic open)
- Next ~14 hours: trade (clean mid-session)
- Last 90 min: skip (noisy close)
- Total tradeable: ~12.5 hours of 23 hour session

This is a simple, data-driven hypothesis: the signal is there, but some times of day
are noisier than others. Trial #1 found the signal; this refines WHEN to use it.
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
        slow_threshold: float = 0.02,
        session_start_skip_ns: int = 2700_000_000_000,  # 45 minutes in nanoseconds
        session_end_skip_ns: int = 5400_000_000_000,    # 90 minutes in nanoseconds
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
            slow_threshold: Minimum slow imbalance magnitude required
            session_start_skip_ns: Skip trading for this duration after session open
            session_end_skip_ns: Skip trading for this duration before session close
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
        self.slow_threshold = slow_threshold
        self.session_start_skip_ns = session_start_skip_ns
        self.session_end_skip_ns = session_end_skip_ns
        self.size = size
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

        # SESSION TIMING FILTER: Skip if too close to session boundaries
        time_since_open = ctx.ts - ctx.session_start
        time_until_close = ctx.session_end - ctx.ts
        
        # Skip if in the first session_start_skip_ns (45 minutes)
        if time_since_open < self.session_start_skip_ns:
            return
        
        # Skip if in the last session_end_skip_ns (90 minutes)
        if time_until_close < self.session_end_skip_ns:
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

        # Entry logic (trial #1 base with session timing filter):
        # - Fast imbalance must exceed adaptive threshold
        # - Slow imbalance must be in same direction and exceed slow_threshold
        # - Must be in the "sweet spot" middle hours of the session

        if fast_imbalance > threshold and slow_imbalance > self.slow_threshold:
            # Strong buying pressure on fast, confirmed by slow, during tradeable hours
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif fast_imbalance < -threshold and slow_imbalance < -self.slow_threshold:
            # Strong selling pressure on fast, confirmed by slow, during tradeable hours
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
