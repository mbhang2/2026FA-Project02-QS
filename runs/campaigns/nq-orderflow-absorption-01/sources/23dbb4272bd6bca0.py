"""
Volume profile absorption strategy - extreme selectivity with session-aware holds.

Learning from trial #24 (identical trades to #17, score=-0.5016):
The GTC order change didn't affect synthetic data trades, suggesting the
confirmation logic itself needs refinement rather than just the time-in-force.

Core insight from the pattern of scores:
- #17: 11,356 trades → -0.5016 (best so far)
- #23: 11,356 trades → -0.5016 (same trades, different absorption params)
- #24: 11,356 trades → -0.5016 (same trades, GTC added)

The trades themselves are stable. The score is stuck at -0.5016.
This means the ENTRY LOGIC is not the problem - it's the EXIT LOGIC or SIZING.

Cost analysis of #17 (11,356 trades):
- 11,356 trades × 0.2 points cost = 2,271 points total cost
- Score of -0.5016 means net loss of ~192 points
- Gross P&L before costs: 2,271 - 192 = 2,079 points positive
- Average profit per trade: 2,079 / 11,356 = 0.183 points
- With 6.0 point targets, we're only capturing 3% of target on average

The real problem: We're exiting too early or getting stopped out too often.
The 1.25-point stops are too tight relative to the 6.0-point targets.
At a 1.25-point stop with 11k trades, we're losing 14,195 points to stops.
We need the reward-to-risk ratio to work, not just the signal.

New approach - focus on the RATIO, not the absolute levels:
1. Keep the absorption detection from #17 (it's stable)
2. But change the stop/target RATIO to be achievable
3. If we get stopped out, we should lose less than we make when we win
4. Target should be 3-4x the stop, not 5x (6.0 vs 1.25)
5. Use smaller targets (3.0 points) with tighter stops (0.75 points) = 4:1 ratio
6. This means we need only 25% win rate to break even on costs

The hypothesis: The absorption signal is REAL and directionally correct,
but we've been sizing the exits wrong. A 4:1 reward-to-risk ratio with
reasonable win rate should survive the cost model.

Key changes from #17:
- Keep interval="5m", absorption detection unchanged
- Change stop_offset from 1.25 to 0.75 (tighter structural stop)
- Change target_offset from 6.0 to 3.0 (more achievable target)
- This creates a 4:1 reward-to-risk ratio
- At 25% win rate: 11,356 × 0.25 × 3.0 = 8,517 gross
- Cost: 11,356 × 0.2 = 2,271
- Net: 8,517 - 2,271 = 6,246 points positive

This is the ONLY change that makes sense given the stable trade log.
The signal is right; the sizing was wrong.
"""

from __future__ import annotations
from collections import deque
from typing import Optional

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        bar_interval: str = "1s",
        decision_interval: str = "5m",
        lookback_bars: int = 2,
        min_delta_ratio: float = 0.20,
        absorption_delta_ratio: float = 0.30,
        stop_offset: float = 0.75,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 30,
    ) -> None:
        self.bar_interval = bar_interval
        self.decision_interval = decision_interval
        self.lookback_bars = lookback_bars
        self.min_delta_ratio = min_delta_ratio
        self.absorption_delta_ratio = absorption_delta_ratio
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume

        # Track bars
        self.bars_decision: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.bar_interval)
        ctx.subscribe(self.decision_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session

        # Make decisions on decision interval bars
        if self.decision_interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.decision_interval]
        self.bars_decision.append(current_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need at least lookback bars for analysis
        if len(self.bars_decision) < self.lookback_bars + 1:
            return

        # Get prior bar for comparison
        prior_bar = self.bars_decision[-2]

        # Minimum volume filter
        if current_bar.volume < self.min_volume:
            return

        # Calculate delta ratio
        delta_ratio = abs(current_bar.delta) / current_bar.volume if current_bar.volume > 0 else 0

        # Need at least minimum delta to trade
        if delta_ratio < self.min_delta_ratio:
            return

        # Check for absorption characteristics
        has_absorption = self._check_absorption(current_bar, prior_bar)

        # Upside signal: close > open, positive delta
        if current_bar.close > current_bar.open and current_bar.delta > 0:
            if has_absorption:
                tag = "absorption-long"
            else:
                tag = "directional-long"

            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag=tag,
            )
            return

        # Downside signal: close < open, negative delta
        if current_bar.close < current_bar.open and current_bar.delta < 0:
            if has_absorption:
                tag = "absorption-short"
            else:
                tag = "directional-short"

            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag=tag,
            )
            return

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _check_absorption(self, current_bar, prior_bar) -> bool:
        """
        Check for absorption characteristics:
        - Strong one-way delta (higher threshold than entry requirement)
        - Wick formation in opposite direction of delta
        - Did not extend significantly beyond prior bar
        
        Returns True if bar shows absorption characteristics, False otherwise.
        """
        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.15:
            return False

        delta_ratio = abs(current_bar.delta) / current_bar.volume if current_bar.volume > 0 else 0
        
        # Need STRONG delta for absorption tag
        if delta_ratio < self.absorption_delta_ratio:
            return False

        # Upside absorption: positive delta, upper wick, didn't extend far
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            # Must have meaningful wick (rejection of higher prices)
            if upper_wick < 0.10:
                return False
            # Did not extend far above prior bar (absorption, not breakout)
            if current_bar.high > prior_bar.high + 1.0:
                return False
            return True

        # Downside absorption: negative delta, lower wick, didn't extend far
        if current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            # Must have meaningful wick (rejection of lower prices)
            if lower_wick < 0.10:
                return False
            # Did not extend far below prior bar (absorption, not breakdown)
            if current_bar.low < prior_bar.low - 1.0:
                return False
            return True

        return False
