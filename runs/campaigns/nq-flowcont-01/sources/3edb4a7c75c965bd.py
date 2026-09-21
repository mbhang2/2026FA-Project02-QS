"""
Order flow continuation with reduced trading frequency and tighter risk management.

Analysis of trial #1 (best score: -0.4823) vs recent trials:
- Trial #1: score -0.4823, ~35k trades - adaptive threshold + multi-timeframe
- Trial #25: score -0.9178, ~48k trades - momentum filter increased trades
- Trial #24: score -0.9702, ~103k trades - trailing stops made it much worse
- Trial #23: score -0.9578, ~104k trades - signal strength scaling failed

Key insight: Trial #1 is the clear winner. Every modification has degraded performance.
The core signal (adaptive volatility threshold + multi-timeframe confirmation) works.

The problem: We're trading too frequently and commission is killing us.
- 35k trades × 0.2 points commission = 7,000 points cost
- Our gross P&L must fight this, resulting in -0.4823 net

New approach - AGGRESSIVE SIGNAL FILTERING:
Instead of trying to improve exits or add momentum filters (which increased trades),
let's be much more selective about WHICH signals we trade:

1. Keep trial #1's core signal completely unchanged:
   - adaptive volatility threshold (0.12 base)
   - multi-timeframe confirmation (fast + slow imbalance)
   - lookback_fast=5, lookback_slow=4
   
2. Add STRICT ENTRY FILTERS to reduce trade count:
   a) Only trade when both fast AND slow imbalance are STRONG (not just directional)
      - Require slow_imbalance > 0.05 for long, < -0.05 for short (not just > 0)
      - This filters out weak slow-timeframe confirmations
   
   b) Add a PRICE ACTION CONFIRMATION:
      - For long: require the last bar's close to be above its open (bullish)
      - For short: require the last bar's close to be below its open (bearish)
      - This ensures price is actually moving in the direction of flow
   
   c) Add a SESSION FILTER:
      - Skip the first 30 minutes of the session (overlapping orders, wide spreads)
      - Skip the last 30 minutes of the session (forced liquidations, wide spreads)
      - Trade only during the liquid middle of the session
   
3. Keep size=1 and brackets from trial #1 (stop_offset=0.8, target_offset=1.2)

4. Expected outcome:
   - Reduce trades from 35k to ~15-20k (more aggressive filtering)
   - Save 3,000-5,000 points on commission
   - Keep only the highest-quality signals
   - Expected score improvement: -0.4823 → potentially -0.25 to -0.35

The logic: Trial #1's signal works but we're trading weak instances.
By requiring BOTH strong fast flow AND strong slow confirmation AND bullish/bearish
price action AND liquid session times, we trade only the clearest, highest-conviction
setups. Fewer trades × higher win rate on quality signals = better net P&L.
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
        slow_threshold: float = 0.05,
        size: int = 1,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
        session_start_minutes: int = 30,
        session_end_minutes: int = 30,
    ):
        """
        Args:
            fast_interval: Fast timeframe for entry signals
            slow_interval: Slow timeframe for trend filter
            lookback_fast: Bars to measure flow over on fast timeframe
            lookback_slow: Bars to measure trend over on slow timeframe
            base_threshold: Base imbalance threshold (adjusted by volatility)
            volatility_window: Bars to measure volatility over
            slow_threshold: Minimum slow imbalance strength (not just direction)
            size: Position size
            stop_offset: Stop loss distance from entry
            target_offset: Profit target distance from entry
            session_start_minutes: Minutes to skip after session open
            session_end_minutes: Minutes to skip before session close
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_fast = lookback_fast
        self.lookback_slow = lookback_slow
        self.base_threshold = base_threshold
        self.volatility_window = volatility_window
        self.slow_threshold = slow_threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.session_start_minutes = session_start_minutes
        self.session_end_minutes = session_end_minutes

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

        # SESSION TIMING FILTER: Skip first and last N minutes of session
        time_since_session_open = ctx.ts - ctx.session_start
        time_until_session_close = ctx.session_end - ctx.ts
        
        # Convert to minutes (nanoseconds / 60 / 1_000_000_000)
        minutes_since_open = time_since_session_open / 60_000_000_000
        minutes_until_close = time_until_session_close / 60_000_000_000
        
        if minutes_since_open < self.session_start_minutes:
            return  # Too early in session
        if minutes_until_close < self.session_end_minutes:
            return  # Too late in session

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

        # Get the last fast bar for price action confirmation
        last_fast_bar = fast_history[-1]
        bullish_price_action = last_fast_bar.close > last_fast_bar.open
        bearish_price_action = last_fast_bar.close < last_fast_bar.open

        # STRICT ENTRY FILTERS:
        # 1. Fast imbalance must exceed adaptive threshold
        # 2. Slow imbalance must be STRONG in the same direction (not just > 0)
        # 3. Price action must confirm the direction

        if (fast_imbalance > threshold and 
            slow_imbalance > self.slow_threshold and 
            bullish_price_action):
            # Strong buying pressure on fast, strong slow confirmation, bullish price
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
        elif (fast_imbalance < -threshold and 
              slow_imbalance < -self.slow_threshold and 
              bearish_price_action):
            # Strong selling pressure on fast, strong slow confirmation, bearish price
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
