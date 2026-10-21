"""
Volume profile absorption strategy - focus on entry timing and multi-bar holds.

Analysis of trial #15 (score=-0.9148, trades=103467):
- High frequency trading at 103k trades across 384 sessions (~270/session)
- Cost model of 0.2 points per round trip (0.1 commission each way + spread)
- Even with 3-point targets, this frequency cannot survive costs
- Score of -0.9148 means losing ~91 cents per point of exposure

Previous attempt (unpublished) tried:
- Strict absorption detection (high delta ratio, wick formation)
- VPOC-based level filtering
- One entry per session limit
- But generated too few trades (likely under 192 session floor)

The core problem: We're oscillating between two extremes:
1. Trade every bar with momentum → 100k+ trades → cost kills it
2. Trade only "perfect" absorption → too few trades → under floor

Key insight from campaign brief:
- "A rule that acts a handful of times and closes the same day will not be 
   measurable here, and that outcome is the rule's fault and not the floor's."
- Multi-session holds are required to reach 50% floor (192 sessions)
- Need 5-15 trades per session AVERAGE, but some should be multi-session holds

New approach - balance frequency with selectivity:
1. Trade on directional momentum (will trigger frequently enough)
2. Filter entries by absorption characteristics (wick + delta)
3. Use LARGER targets (5+ points) to survive costs
4. Structural stops just beyond the absorption bar
5. This generates ~20-30 trades per session, many held multi-session
6. Absorption tag identifies which trades show the signal

The hypothesis test: Do absorption-tagged trades outperform directional-only?
If absorption precedes moves, absorption-tagged trades should have better returns.
If not, we learn that absorption is not predictive despite the logic.

Implementation changes from previous:
- Looser delta ratio requirement (0.20 instead of 0.35) to generate more trades
- No "one entry per session" limit - let multiple setups trade
- Targets 5-7 points (up from 3) to better survive cost at this frequency
- Stops 1-1.5 points (structural, just beyond absorption bar)
- Track both absorption and non-absorption entries separately via tags
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
        stop_offset: float = 1.25,
        target_offset: float = 6.0,
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
