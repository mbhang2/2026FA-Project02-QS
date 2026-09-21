"""
Order flow continuation with multi-bar flow momentum and adaptive entry sizing.

Analysis of 30 trials reveals the core issue: Trial #1 (score -0.4823) succeeds
because it trades SELECTIVELY with a TIGHT SIGNAL, not because of complexity.

Pattern analysis:
- Trial #1: -0.4823, ~35k trades (best)
  - Core: adaptive volatility threshold (0.12) + multi-timeframe (1m + 15m)
  - Tight brackets (stop=0.8, target=1.2)
  - Selective entry = fewer trades, better quality
  
- Trial #30: -0.9738, ~195k trades (worst recently)
  - Flow acceleration idea increased trades 5.5x over trial #1
  - More trades = more commission drag
  - Worse score despite "better entry timing"

- Trial #29: -0.9232, ~56k trades
  - Session timing filters didn't help
  - Volatility regime filter wasn't selective enough

Key insight: The problem is NOT signal mechanics. Trial #1's core signal works.
The problem is OVER-TRADING. Every modification that increases trade frequency
degrades the score. Trial #1's ~35k trades is the sweet spot.

Why trial #1 works:
1. High base threshold (0.12) = only extreme imbalance triggers entry
2. Multi-timeframe confirmation = filters out countertrend noise
3. Tight brackets (0.8/1.2) = good risk:reward, tight stops reduce losses
4. Adaptive volatility = scales threshold to market conditions

What failed in subsequent trials:
- Flow acceleration (trial #30): Catches too many marginal buildups, 5.5x trades
- Regime filters (trial #29): Still trading too much, filters not selective
- Momentum additions (trial #28): Marginal signals, 5x trades
- Wider brackets (trial #27): Stop-outs on normal volatility

New hypothesis: Trial #1 is ALREADY OPTIMAL for this signal.
The only improvement: TIGHTEN IT FURTHER by being more selective.

Strategy (ultra-selective version of trial #1):
1. Keep trial #1's exact logic (adaptive threshold + multi-timeframe)
2. Keep trial #1's brackets (stop=0.8, target=1.2)
3. CHANGE: Require SUSTAINED flow, not just one bar
   - Measure: is the current bar's imbalance above threshold?
   - AND: was the previous bar's imbalance in the same direction?
   - This filters out one-bar spikes that reverse immediately
4. Increase base threshold slightly (0.12 → 0.14) for even more selectivity
5. Require slow imbalance to be STRONG (not just > 0), e.g., > 0.05
6. This should reduce trades from 35k (trial #1) to ~25k while improving win rate

The logic: Trial #1 is good. Make it MORE selective by requiring sustained flow
and stronger multi-timeframe confirmation. Fewer, higher-quality trades = better
net P&L after commission.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "15m",
        lookback_fast: int = 5,
        lookback_slow: int = 4,
        base_threshold: float = 0.14,
        volatility_window: int = 20,
        size: int = 1,
        stop_offset: float = 0.8,
        target_offset: float = 1.2,
        slow_threshold: float = 0.05,
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
            slow_threshold: Minimum slow imbalance magnitude to confirm trend
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
        self.slow_threshold = slow_threshold

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

        # Need enough history: lookback_fast + 1 for sustained flow check
        if len(fast_history) < self.lookback_fast + 1:
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

        # Check for SUSTAINED flow: current bar + previous bar both show flow in same direction
        # Get last 2 bars to verify sustained flow
        recent_bars = fast_history.last(2)
        if len(recent_bars) >= 2:
            current_bar = recent_bars[-1]
            prior_bar = recent_bars[-2]
            
            # Normalize by volume
            if current_bar.volume > 0:
                current_imbalance = current_bar.delta / current_bar.volume
            else:
                current_imbalance = 0.0
            
            if prior_bar.volume > 0:
                prior_imbalance = prior_bar.delta / prior_bar.volume
            else:
                prior_imbalance = 0.0
        else:
            current_imbalance = 0.0
            prior_imbalance = 0.0

        # Get slow timeframe trend for confirmation
        slow_window = slow_history.last(self.lookback_slow)
        slow_delta = 0.0
        slow_volume = 0.0

        for bar in slow_window:
            slow_delta += bar.delta
            slow_volume += bar.volume

        if slow_volume <= 0:
            return

        slow_imbalance = slow_delta / slow_volume

        # Entry logic (ultra-selective version of trial #1):
        # - Current bar imbalance must exceed threshold
        # - Prior bar imbalance must be in same direction (sustained flow)
        # - Slow imbalance must be strong in same direction (strong confirmation)
        # This filters one-bar spikes and countertrend noise

        if (current_imbalance > threshold and 
            prior_imbalance > 0 and 
            slow_imbalance > self.slow_threshold):
            # Sustained buying pressure on fast, strongly confirmed by slow
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-sustained-long",
            )
        elif (current_imbalance < -threshold and 
              prior_imbalance < 0 and 
              slow_imbalance < -self.slow_threshold):
            # Sustained selling pressure on fast, strongly confirmed by slow
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-sustained-short",
            )

    def on_fill(self, ctx) -> None:
        # No special fill handling needed; bracket handles protection
        pass

    def on_lifecycle(self, ctx) -> None:
        # No special lifecycle handling needed
        pass
